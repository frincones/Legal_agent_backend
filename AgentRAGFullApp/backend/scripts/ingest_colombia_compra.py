"""
Ejecutor del scraper Colombia Compra Eficiente.

Descarga los pliegos tipo + modelos de contratos estatales (10 docs seed)
y los persiste en `template_candidates` para revision del curador.

Usage:
    python -m scripts.ingest_colombia_compra [--limit N]

Requisitos:
    DATABASE_URL en .env
    Conexion a internet (descarga PDFs de colombiacompra.gov.co)

Stats al finalizar:
    items_emitted   cuantos docs se insertaron
    items_skipped   duplicados (mismo content_hash)
    errors          fallos de descarga / parse
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

import asyncpg
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ingest_colombia_compra")


async def main(limit: int = 40) -> int:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL no configurada en .env")
        return 1

    # Importar scraper
    try:
        from legal_sources.templates.colombia_compra_scraper import ColombiaCompraScraper
    except Exception as e:
        logger.error("Import del scraper fallo: %s", e)
        return 2

    # Conectar a DB
    try:
        pool = await asyncpg.create_pool(
            db_url,
            min_size=1,
            max_size=2,
            command_timeout=120,
            statement_cache_size=0,
        )
    except Exception as e:
        logger.error("Conexion DB fallo: %s", e)
        return 3

    # Verificar que template_candidates existe
    async with pool.acquire() as conn:
        exists = await conn.fetchval("""
            SELECT EXISTS(
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'template_candidates'
            )
        """)
        if not exists:
            logger.error("Tabla template_candidates no existe. Aplicar migraciones primero.")
            await pool.close()
            return 4

    # Iniciar ingest_run para tracking
    run_id = None
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow("""
                INSERT INTO ingest_runs (source, triggered_by, started_at)
                VALUES ($1, 'manual', now())
                RETURNING id
            """, "colombia_compra")
            run_id = str(row["id"])
            logger.info("ingest_run created: %s", run_id)
        except Exception as e:
            logger.warning("No se pudo crear ingest_run: %s (tabla no existe?)", e)

    # Ejecutar scraper
    scraper = ColombiaCompraScraper()
    emitted = 0
    skipped = 0
    failed = 0
    errors: list[str] = []

    async for candidate in scraper.fetch(limit=limit):
        try:
            async with pool.acquire() as conn:
                # Check dedup por (source, source_ref)
                existing = await conn.fetchval("""
                    SELECT id FROM template_candidates
                    WHERE source = $1 AND source_ref = $2
                    LIMIT 1
                """, candidate.source, candidate.source_ref)

                if existing:
                    logger.info("Skip duplicate: %s", candidate.source_ref)
                    skipped += 1
                    continue

                # Insertar
                await conn.execute("""
                    INSERT INTO template_candidates (
                        id, source, source_ref, source_url,
                        raw_text, normalized_md,
                        suggested_materia, suggested_doc_type, suggested_subtype,
                        suggested_norms, metadata,
                        created_at
                    )
                    VALUES (
                        gen_random_uuid(), $1, $2, $3,
                        $4, $5,
                        $6, $7, $8,
                        $9, $10::jsonb,
                        now()
                    )
                """,
                    candidate.source,
                    candidate.source_ref,
                    candidate.source_url,
                    candidate.raw_text[:50000],  # truncate por si es muy grande
                    candidate.normalized_md,
                    candidate.suggested_materia,
                    candidate.suggested_doc_type,
                    candidate.suggested_subtype,
                    candidate.suggested_norms,
                    __import__("json").dumps(candidate.metadata or {}),
                )
                emitted += 1
                logger.info("Inserted: %s [%s]", candidate.suggested_subtype, candidate.source_ref)
        except Exception as e:
            failed += 1
            err_msg = f"{candidate.source_ref}: {e}"
            errors.append(err_msg)
            logger.error("Insert fallo: %s", err_msg)

    # Finalizar ingest_run
    if run_id:
        async with pool.acquire() as conn:
            try:
                await conn.execute("""
                    UPDATE ingest_runs
                    SET completed_at = now(),
                        docs_processed = $1,
                        docs_failed = $2,
                        docs_skipped = $3,
                        stats_jsonb = $4::jsonb
                    WHERE id = $5
                """,
                    emitted, failed, skipped,
                    __import__("json").dumps({"errors": errors[:20]}),
                    run_id,
                )
            except Exception as e:
                logger.warning("Update ingest_run fallo: %s", e)

    await pool.close()

    print()
    print("=" * 60)
    print("INGEST COLOMBIA COMPRA · COMPLETADO")
    print("=" * 60)
    print(f"  Emitted:  {emitted}")
    print(f"  Skipped:  {skipped} (duplicados)")
    print(f"  Failed:   {failed}")
    if errors:
        print(f"\nPrimeros errores:")
        for err in errors[:5]:
            print(f"  - {err}")
    print()
    print(f"Verificar en DB:")
    print(f"  SELECT count(*) FROM template_candidates WHERE source='colombia_compra';")
    print(f"  Esperado: {emitted}")
    print()

    return 0 if failed == 0 else 5


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest Colombia Compra templates")
    parser.add_argument("--limit", type=int, default=40, help="Max docs to ingest")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.limit)))
