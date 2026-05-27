"""
Auto-migration runner para migraciones Sprint L-DOC y Sprint M (block streaming).

Se ejecuta desde el lifespan del backend al iniciar.
Idempotente: usa IF NOT EXISTS, DO $$ EXCEPTION BLOCK, etc.
Si la migracion ya esta aplicada, no hace nada.

Solo aplica migraciones controladas por LexAI v2 (no toca migraciones legacy).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

# Lista de migraciones a aplicar en orden.
# Cada migracion DEBE ser idempotente.
SPRINT_L_DOC_MIGRATIONS = [
    "2026_05_25_sprint_l_doc.sql",
    "2026_05_26_sprint_m_block_streaming.sql",
    "2026_05_25_sprint_m14_evidence_columns.sql",
    "2026_05_25_sprint_m15_health_view.sql",
    "2026_05_25_sprint_m17_fuente_urls.sql",
    "2026_05_25_sprint_m18_norma_url_index.sql",
    "2026_05_27_sprint_m19_chat_messages.sql",
]

# Tablas que las migraciones crean — usadas para detectar si ya están aplicadas.
EXPECTED_TABLES = {
    # Sprint L-DOC
    "ingest_queue",
    "ingest_runs",
    "pipeline_logs",
    "template_sections_catalog",
    "generated_document_sections",
    "document_section_revisions",
    "document_quality_scores",
    "template_usage_stats",
    # Sprint M (block streaming)
    "document_blocks",
    "generation_audit",
    "template_catalog",
    "citation_verifications",
    "document_versions",
    # Sprint M14 (shadow mode + evidence)
    "verification_shadow_diffs",
}


_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sha256 TEXT
);
"""


async def _applied_migrations(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch("SELECT filename FROM schema_migrations")
    return {r["filename"] for r in rows}


async def run_sprint_l_doc_migrations(pool: asyncpg.Pool) -> None:
    """
    Aplica todas las migraciones Sprint L-DOC / M / M17 que aún no estén
    registradas en `schema_migrations`. Idempotente: cada archivo se
    ejecuta exactamente una vez por instancia.

    Compatibilidad con prod existente: si una migración crea tablas
    que YA existen (porque se aplicó antes de que hubiera registro),
    los archivos usan `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ...
    IF NOT EXISTS` así que la re-ejecución es no-op. Después la
    marcamos como aplicada y nunca más corre.
    """
    if os.getenv("DISABLE_AUTO_MIGRATE", "false").lower() == "true":
        logger.info("auto_migrate: DISABLE_AUTO_MIGRATE=true, skipping")
        return

    schemas_dir = Path(__file__).parent / "schemas"

    try:
        async with pool.acquire() as conn:
            # 1. Garantizar que existe la tabla de tracking
            await conn.execute(_SCHEMA_MIGRATIONS_DDL)
            already_applied = await _applied_migrations(conn)
    except Exception as e:
        logger.warning("auto_migrate: no se pudo inicializar schema_migrations (%s); skipping", e)
        return

    pending = [f for f in SPRINT_L_DOC_MIGRATIONS if f not in already_applied]
    if not pending:
        logger.info("auto_migrate: todas las migraciones aplicadas (%d/%d)",
                    len(already_applied & set(SPRINT_L_DOC_MIGRATIONS)),
                    len(SPRINT_L_DOC_MIGRATIONS))
        return

    logger.warning("auto_migrate: aplicando %d migraciones pendientes: %s",
                   len(pending), pending)

    for filename in pending:
        sql_path = schemas_dir / filename
        if not sql_path.exists():
            logger.error("auto_migrate: archivo no encontrado: %s", sql_path)
            continue

        sql = sql_path.read_text(encoding="utf-8")
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (filename) VALUES ($1) "
                        "ON CONFLICT (filename) DO NOTHING",
                        filename,
                    )
            logger.info("auto_migrate: aplicada %s (%d bytes)", filename, len(sql))
        except Exception as e:
            logger.error("auto_migrate: error aplicando %s: %s", filename, e)
            # No re-raise: backend arranca igual; otras migraciones siguen intentándose.

    # Re-verificar tablas core
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = ANY($1::text[])
            """, list(EXPECTED_TABLES))
            existing_after = {r["table_name"] for r in rows}
        still_missing = EXPECTED_TABLES - existing_after
        if still_missing:
            logger.error("auto_migrate: tablas core aun faltantes: %s", still_missing)
        else:
            logger.info("auto_migrate: todas las tablas core OK (%d)", len(EXPECTED_TABLES))
    except Exception as e:
        logger.warning("auto_migrate: error re-verificando tablas: %s", e)
