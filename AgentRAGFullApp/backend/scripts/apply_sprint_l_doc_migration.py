"""
Apply Sprint L-DOC migration to Supabase Postgres.

Usage:
    python -m scripts.apply_sprint_l_doc_migration

Reads DATABASE_URL from .env. Idempotente: si las tablas ya existen,
no falla (usa IF NOT EXISTS y DO $$ EXCEPTION BLOCK).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv()

MIGRATION_FILE = Path(__file__).parent.parent / "storage" / "schemas" / "2026_05_25_sprint_l_doc.sql"

EXPECTED_TABLES = [
    "ingest_queue",
    "ingest_runs",
    "pipeline_logs",
    "template_sections_catalog",
    "generated_document_sections",
    "document_section_revisions",
    "document_quality_scores",
    "template_usage_stats",
]


async def main() -> int:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL no configurada en .env", file=sys.stderr)
        return 1

    sql = MIGRATION_FILE.read_text(encoding="utf-8")
    print(f"Aplicando migracion: {MIGRATION_FILE.name}")
    print(f"  Tamano: {len(sql)} bytes")

    try:
        # Usar create_pool igual que el backend en produccion
        pool = await asyncpg.create_pool(
            db_url,
            min_size=1,
            max_size=1,
            command_timeout=120,
            statement_cache_size=0,
        )
    except Exception as e:
        print(f"ERROR conectando a DB: {e}", file=sys.stderr)
        return 2

    conn = await pool.acquire()

    try:
        # Ejecutar migracion completa
        await conn.execute(sql)
        print("Migracion aplicada sin errores.")

        # Validar tablas creadas
        rows = await conn.fetch("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = ANY($1::text[])
            ORDER BY table_name
        """, EXPECTED_TABLES)
        found = {r["table_name"] for r in rows}
        missing = set(EXPECTED_TABLES) - found

        print(f"\nTablas verificadas: {len(found)}/{len(EXPECTED_TABLES)}")
        for t in EXPECTED_TABLES:
            mark = "OK" if t in found else "MISSING"
            print(f"  [{mark}] {t}")

        if missing:
            print(f"\nERROR: Tablas faltantes: {missing}", file=sys.stderr)
            return 3

        # Validar vista
        view_exists = await conn.fetchval("""
            SELECT EXISTS(
                SELECT 1 FROM information_schema.views
                WHERE table_schema = 'public' AND table_name = 'ingest_dashboard'
            )
        """)
        print(f"  [{('OK' if view_exists else 'MISSING')}] ingest_dashboard (view)")

        # Test query a ingest_dashboard
        count = await conn.fetchval("SELECT count(*) FROM ingest_dashboard")
        print(f"\ningest_dashboard tiene {count} filas (esperado 0 inicialmente)")

        print("\nMigracion Sprint L-DOC aplicada con exito.")
        return 0
    except Exception as e:
        print(f"ERROR ejecutando migracion: {e}", file=sys.stderr)
        return 4
    finally:
        await pool.release(conn)
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
