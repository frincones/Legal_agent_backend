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


async def run_sprint_l_doc_migrations(pool: asyncpg.Pool) -> None:
    """
    Verifica si las tablas Sprint L-DOC existen. Si faltan, aplica
    el SQL completo. Si todas existen, no hace nada.
    """
    if os.getenv("DISABLE_AUTO_MIGRATE", "false").lower() == "true":
        logger.info("auto_migrate: DISABLE_AUTO_MIGRATE=true, skipping")
        return

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = ANY($1::text[])
            """, list(EXPECTED_TABLES))
            existing = {r["table_name"] for r in rows}
    except Exception as e:
        logger.warning("auto_migrate: no se pudo verificar tablas (%s); skipping", e)
        return

    missing = EXPECTED_TABLES - existing
    if not missing:
        logger.info("auto_migrate: Sprint L-DOC ya aplicado (%d/%d tablas)",
                    len(existing), len(EXPECTED_TABLES))
        return

    logger.warning("auto_migrate: faltan tablas Sprint L-DOC: %s. Aplicando migracion...",
                   missing)

    schemas_dir = Path(__file__).parent / "schemas"
    for filename in SPRINT_L_DOC_MIGRATIONS:
        sql_path = schemas_dir / filename
        if not sql_path.exists():
            logger.error("auto_migrate: archivo no encontrado: %s", sql_path)
            continue

        sql = sql_path.read_text(encoding="utf-8")
        try:
            async with pool.acquire() as conn:
                await conn.execute(sql)
            logger.info("auto_migrate: aplicada %s (%d bytes)", filename, len(sql))
        except Exception as e:
            logger.error("auto_migrate: error aplicando %s: %s", filename, e)
            # No re-raise: el backend debe iniciar igual; las tablas faltantes
            # se reportaran como 500 en los endpoints que las necesitan.

    # Re-verificar
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
            logger.error("auto_migrate: tablas aun faltantes despues de migrar: %s",
                         still_missing)
        else:
            logger.info("auto_migrate: Sprint L-DOC aplicado correctamente (%d tablas)",
                        len(EXPECTED_TABLES))
    except Exception as e:
        logger.warning("auto_migrate: error re-verificando tablas: %s", e)
