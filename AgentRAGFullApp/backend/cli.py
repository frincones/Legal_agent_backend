"""CLI interface for ingestion and interactive chat."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv

# Load .env before any other imports that might read env vars
load_dotenv()

from config.schema import load_config
from utils.db import get_storage, close_storage
from utils.logger import setup_logger


async def run_ingest(args):
    """Run document ingestion from CLI."""
    from ingestion.pipeline import IngestionPipeline

    config = load_config(args.config)
    storage = await get_storage(config.storage)

    pipeline = IngestionPipeline(config, storage)

    def progress(file_path, current, total):
        print(f"  [{current}/{total}] Processing: {os.path.basename(file_path)}")

    result = await pipeline.ingest_directory(
        directory=args.directory,
        clean_before=args.clean,
        progress_callback=progress,
    )

    print(f"\nIngestion complete:")
    print(f"  Documents: {result.successful}/{result.total_documents}")
    print(f"  Chunks: {result.total_chunks}")
    print(f"  Duration: {result.duration_seconds:.1f}s")

    if result.errors:
        print(f"\n  Errors ({result.failed}):")
        for err in result.errors:
            print(f"    - {err['file']}: {err['error']}")

    await close_storage()


async def run_chat(args):
    """Run interactive chat from CLI."""
    from agent.base_agent import RAGAgent

    config = load_config(args.config)
    storage = await get_storage(config.storage)
    agent = RAGAgent(config, storage)

    print(f"\n{config.agent.name} ({config.agent.role})")
    print("Type 'quit' to exit, 'reset' to clear history\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit"):
            break
        if user_input.lower() == "reset":
            agent.reset_session()
            print("[Session reset]\n")
            continue

        print(f"\n{config.agent.name}: ", end="", flush=True)

        async for chunk in agent.chat_stream(user_input):
            print(chunk, end="", flush=True)

        print("\n")

    await close_storage()
    print("\nGoodbye!")


async def run_init_db(args):
    """Initialize the database schema."""
    from pathlib import Path

    config = load_config(args.config)
    storage = await get_storage(config.storage)

    schema_path = Path(__file__).parent / "storage" / "schemas" / "init.sql"
    if not schema_path.exists():
        print(f"Schema file not found: {schema_path}")
        await close_storage()
        return

    sql = schema_path.read_text(encoding="utf-8")

    from storage.postgres import PostgresStorage
    if isinstance(storage, PostgresStorage):
        async with storage.pool.acquire() as conn:
            await conn.execute(sql)
        print("Database schema initialized successfully!")
    else:
        print("Database initialization only supported for PostgreSQL storage.")

    await close_storage()


def main():
    parser = argparse.ArgumentParser(description="Agent RAG Template CLI")
    parser.add_argument("--config", default=None, help="Path to config YAML file")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Ingest command
    ingest_parser = subparsers.add_parser("ingest", help="Ingest documents")
    ingest_parser.add_argument("directory", help="Directory containing documents")
    ingest_parser.add_argument("--clean", action="store_true", help="Clear existing data before ingestion")

    # Chat command
    subparsers.add_parser("chat", help="Start interactive chat")

    # Init DB command
    subparsers.add_parser("init-db", help="Initialize database schema")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return

    setup_logger(level=os.getenv("LOG_LEVEL", "INFO"))

    if args.command == "ingest":
        asyncio.run(run_ingest(args))
    elif args.command == "chat":
        asyncio.run(run_chat(args))
    elif args.command == "init-db":
        asyncio.run(run_init_db(args))


if __name__ == "__main__":
    main()
