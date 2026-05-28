"""Worker que detecta entradas en external_fetch_cache con hit_count > N
y las promueve al core corpus vía pipeline.ingest.

Patrón: APScheduler-like background task que corre cada N segundos.
Marca auto_ingested_at para no procesar la misma entrada dos veces.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

DEFAULT_HIT_THRESHOLD = 3
DEFAULT_BATCH_SIZE = 5
DEFAULT_TICK_INTERVAL_SECONDS = 300  # cada 5 min


async def auto_ingest_tick(
    pool: asyncpg.Pool,
    hit_threshold: int = DEFAULT_HIT_THRESHOLD,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Tick una vez: procesa N entradas pendientes. Devuelve cantidad procesada."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT cache_key, source, url, content_jsonb, content_text, hit_count
                FROM external_fetch_cache
                WHERE hit_count >= $1
                  AND auto_ingested_at IS NULL
                  AND status = 'ok'
                  AND content_text IS NOT NULL
                ORDER BY hit_count DESC
                LIMIT $2
            """, hit_threshold, batch_size)
    except Exception as e:
        logger.warning("auto_ingest_tick query failed: %s", e)
        return 0

    if not rows:
        return 0

    processed = 0
    for r in rows:
        try:
            ok = await _promote_to_core(pool, dict(r))
            if ok:
                processed += 1
                async with pool.acquire() as conn:
                    await conn.execute("""
                        UPDATE external_fetch_cache
                        SET auto_ingested_at = now()
                        WHERE cache_key = $1
                    """, r["cache_key"])
        except Exception as e:
            logger.warning("auto_ingest promote failed for %s: %s", r["cache_key"], e)

    if processed > 0:
        logger.info("auto_ingest: promoted %d entries to core corpus", processed)
    return processed


async def _promote_to_core(pool: asyncpg.Pool, cache_row: dict[str, Any]) -> bool:
    """Inserta el contenido cacheado como nuevo documento en core corpus.

    Best-effort: inserta en documents + crea chunks básicos.
    M19.23.K — Schema de documents NO tiene columna `url`. Se mueve a
    metadata.url para conservar la trazabilidad sin requerir migración.
    """
    try:
        content = cache_row.get("content_text") or ""
        if len(content) < 100:
            return False  # demasiado corto

        source = cache_row.get("source") or "auto_cache"
        url = cache_row.get("url")
        cache_key = cache_row["cache_key"]

        import json as _json
        metadata = {"url": url, "cache_key": cache_key, "promoted_from": "auto_ingest_worker"}

        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO documents (id, title, source, doc_type, content, metadata, created_at)
                VALUES (gen_random_uuid(), $1, $2, $3, $4, $5::jsonb, now())
                ON CONFLICT DO NOTHING
            """,
                f"[auto-cache] {cache_key[:80]}",
                f"auto_cached_{source}",
                "jurisprudencia" if "csj" in source or "cc" in source else "norma",
                content[:50000],  # limit
                _json.dumps(metadata, ensure_ascii=False, default=str))
        return True
    except Exception as e:
        logger.warning("promote_to_core insert failed: %s", e)
        return False


async def start_auto_ingest_worker(
    pool: asyncpg.Pool,
    interval_seconds: int = DEFAULT_TICK_INTERVAL_SECONDS,
) -> asyncio.Task:
    """Inicia el worker como background task. Llamar desde lifespan."""
    async def _loop():
        logger.info("auto_ingest_worker started (interval=%ds)", interval_seconds)
        while True:
            try:
                await auto_ingest_tick(pool)
            except Exception as e:
                logger.warning("auto_ingest_worker tick error: %s", e)
            await asyncio.sleep(interval_seconds)

    task = asyncio.create_task(_loop())
    return task
