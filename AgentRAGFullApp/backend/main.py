"""Agent RAG Template - Main entry point."""

from __future__ import annotations

import logging
import os
import uvicorn

from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Load environment variables from .env file
load_dotenv()

from config.schema import load_config
from utils.db import get_storage, close_storage
from utils.logger import setup_logger

logger = setup_logger("agent-rag", level=os.getenv("LOG_LEVEL", "INFO"))


async def _prewarm_openai():
    """Pre-warm the OpenAI HTTPS connection so the first user request
    doesn't pay the cold-start TLS handshake (~3s on first call).

    We send a tiny embedding request that establishes the keep-alive
    connection in the shared httpx pool. Subsequent requests reuse it.
    """
    try:
        from utils.llm import get_openai_client
        client = get_openai_client()
        # Tiny embedding to warm DNS + TLS + auth
        await client.embeddings.create(
            model="text-embedding-3-small",
            input="warmup",
        )
        logger.info("OpenAI connection pre-warmed")
    except Exception as e:
        logger.warning("OpenAI pre-warm failed (non-fatal): %s", e)


async def _prewarm_reranker():
    """Pre-load the cross-encoder model so the first re-ranking call
    doesn't pay the model download/load cost (~25s on first ever call,
    ~2s on subsequent cold loads after restart).
    """
    try:
        import asyncio
        from retrieval.reranker import _get_reranker

        # Run the synchronous model load in a thread to not block startup
        def _load():
            _get_reranker("cross-encoder/ms-marco-MiniLM-L-6-v2")

        await asyncio.to_thread(_load)
        logger.info("Cross-encoder re-ranker pre-loaded")
    except Exception as e:
        logger.warning("Re-ranker pre-warm failed (non-fatal): %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    config = load_config()
    logger.info("Starting Agent RAG Template: %s (%s)", config.agent.name, config.agent.role)

    # Initialize storage on startup
    storage = await get_storage(config.storage)
    logger.info("Storage initialized: %s", config.storage.provider)

    # Pre-warm OpenAI connection to eliminate cold-start latency on first request
    await _prewarm_openai()

    # Pre-load the cross-encoder reranker model (skip if disabled or via env)
    if config.retrieval.reranking.enabled and os.getenv("SKIP_RERANKER_PREWARM") != "1":
        await _prewarm_reranker()
    else:
        logger.info("Re-ranker prewarm skipped (disabled in config or env)")

    # Initialize legal sources and derogation graph
    try:
        from legal_sources.source_router import LegalSourceRouter
        from derogation.graph import DerogationGraph
        from derogation.vigencia_checker import VigenciaChecker
        from ingestion.embedder import create_embedder
        from api.legal import init_legal_api

        legal_config = getattr(config, 'legal_sources', None)
        legal_config_dict = legal_config.model_dump() if legal_config else {}
        source_router = LegalSourceRouter(legal_config_dict)

        derogation_graph = DerogationGraph(storage.pool)
        vigencia_checker = VigenciaChecker(derogation_graph)
        embedder = create_embedder(
            model=config.ingestion.embedding.model,
            use_cache=True,
        )

        init_legal_api(source_router, derogation_graph, vigencia_checker, storage, embedder)
        logger.info("Legal sources and derogation graph initialized")
    except Exception as e:
        logger.warning("Legal system init failed (non-fatal): %s", e)

    # LexAI · Register Realtime tool implementations
    try:
        from api.voice import register_tool
        from api.citations import (
            research_jurisprudence_tool,
            validate_citation_tool,
            validate_norm_vigencia_tool,
        )
        from api.calc import calc_liquidacion_tool
        from api.matters import open_matter_context_tool
        from api.hitl import request_approval_tool
        from agent.tools.draft_pleading import draft_pleading_tool
        from agent.tools.paralegal_tools import (
            list_my_matters_tool,
            find_client_tool,
            list_upcoming_deadlines_tool,
            add_matter_deadline_tool,
            mark_deadline_done_tool,
            add_matter_note_tool,
            list_matter_documents_tool,
            summarize_document_tool,
            list_pending_hitl_tool,
            get_firm_metrics_tool,
        )

        # Core 7
        register_tool("research_jurisprudence", research_jurisprudence_tool)
        register_tool("validate_citation", validate_citation_tool)
        register_tool("validate_norm_vigencia", validate_norm_vigencia_tool)
        register_tool("calc_liquidacion", calc_liquidacion_tool)
        register_tool("open_matter_context", open_matter_context_tool)
        register_tool("request_human_approval", request_approval_tool)
        register_tool("draft_pleading", draft_pleading_tool)
        # Paralegal-grade extensions (10)
        register_tool("list_my_matters", list_my_matters_tool)
        register_tool("find_client", find_client_tool)
        register_tool("list_upcoming_deadlines", list_upcoming_deadlines_tool)
        register_tool("add_matter_deadline", add_matter_deadline_tool)
        register_tool("mark_deadline_done", mark_deadline_done_tool)
        register_tool("add_matter_note", add_matter_note_tool)
        register_tool("list_matter_documents", list_matter_documents_tool)
        register_tool("summarize_document", summarize_document_tool)
        register_tool("list_pending_hitl", list_pending_hitl_tool)
        register_tool("get_firm_metrics", get_firm_metrics_tool)
        logger.info("LexAI Realtime tools registered (17 total)")
    except Exception as e:
        logger.warning("LexAI tool registration failed (non-fatal): %s", e)

    yield

    # Cleanup on shutdown
    await close_storage()
    logger.info("Shutdown complete")


app = FastAPI(
    title="Agent RAG Template",
    description="Customizable RAG agent with Level 3 ingestion and Level 5 retrieval",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
from api.health import router as health_router
from api.chat import router as chat_router
from api.ingest import router as ingest_router
from api.documents import router as documents_router
from api.sessions import router as sessions_router
from api.usage import router as usage_router
from api.legal import router as legal_router

# LexAI MVP routers (multi-tenant, JWT-protected)
from api.voice import router as voice_router
from api.calc import router as calc_router
from api.citations import router as citations_router
from api.hitl import router as hitl_router
from api.matters import router as matters_router
from api.clients import router as clients_router

app.include_router(health_router, prefix="/api")
app.include_router(chat_router, prefix="/api")
app.include_router(ingest_router, prefix="/api")
app.include_router(documents_router, prefix="/api")
app.include_router(sessions_router, prefix="/api")
app.include_router(usage_router, prefix="/api")
app.include_router(legal_router)  # Already has /api/legal prefix

# LexAI · /v1/* (these routers self-prefix with /v1/...)
app.include_router(voice_router)
app.include_router(calc_router)
app.include_router(citations_router)
app.include_router(hitl_router)
app.include_router(matters_router)
app.include_router(clients_router)


def main():
    """Run the application."""
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))

    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=os.getenv("ENV", "development") == "development",
    )


if __name__ == "__main__":
    main()
