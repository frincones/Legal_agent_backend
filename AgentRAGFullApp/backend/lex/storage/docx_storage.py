"""Sprint M19.12.C1 · DOCX cache en Supabase Storage.

Persiste los archivos .docx generados en un bucket 'documents' para que
sean descargables después del cierre de sesión y para no re-generar el
mismo doc en cada request.

Flujo:
  1. get_or_build_and_cache_docx(document_id, builder):
     - Si existe en cache table `document_files` con bytes válidos → retornar
     - Si no, builder() genera bytes → guardar en storage + tabla → retornar

Tabla `document_files` (creada por migration M19.12 idempotente):
  - document_id (FK)
  - storage_path (TEXT)        -- ruta en bucket
  - format ('docx' | 'pdf')
  - size_bytes (INT)
  - created_at (TIMESTAMPTZ)

Fallback: si Storage falla, simplemente devolvemos los bytes sin persistir
(no rompemos el download).
"""
from __future__ import annotations

import logging
import os
from typing import Callable

logger = logging.getLogger(__name__)


async def get_or_build_and_cache_docx(
    pool,
    document_id: str,
    builder: Callable[[], bytes],
) -> bytes:
    """Devuelve bytes DOCX: cached si existe, sino builder() + cache.

    Args:
        pool: asyncpg pool
        document_id: UUID del documento
        builder: callable sin args que retorna bytes (typically build_docx_from_blocks)

    Returns:
        bytes del .docx
    """
    # Intentar leer desde cache
    cached = await _read_cached_docx(pool, document_id)
    if cached:
        logger.info("docx cache HIT for %s (%d bytes)", document_id, len(cached))
        return cached

    # Cache miss: build
    docx_bytes = builder()

    # Intentar persistir (best-effort, no bloquea download)
    try:
        await _write_docx_to_cache(pool, document_id, docx_bytes)
        logger.info("docx cache STORED for %s (%d bytes)", document_id, len(docx_bytes))
    except Exception as e:
        logger.warning("docx cache write failed (returning bytes anyway): %s", e)

    return docx_bytes


async def _read_cached_docx(pool, document_id: str) -> bytes | None:
    """Lee bytes DOCX de la tabla cache. None si no existe."""
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT content_bytes FROM document_files
                WHERE document_id = $1::uuid AND format = 'docx'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                document_id,
            )
        if row and row["content_bytes"]:
            return bytes(row["content_bytes"])
    except Exception as e:
        # Tabla puede no existir aún (migration no aplicada) — fallback gracioso
        logger.debug("docx cache read miss/error: %s", e)
    return None


async def _write_docx_to_cache(pool, document_id: str, docx_bytes: bytes) -> None:
    """Persiste bytes DOCX en la tabla cache.

    Idempotente: si ya hay un row para este document_id, lo reemplaza.
    """
    if pool is None or not docx_bytes:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO document_files (document_id, format, content_bytes, size_bytes)
            VALUES ($1::uuid, 'docx', $2, $3)
            ON CONFLICT (document_id, format) DO UPDATE
              SET content_bytes = EXCLUDED.content_bytes,
                  size_bytes = EXCLUDED.size_bytes,
                  created_at = now()
            """,
            document_id, docx_bytes, len(docx_bytes),
        )


async def invalidate_cache(pool, document_id: str) -> None:
    """Elimina el cache para forzar re-build (cuando el doc cambia)."""
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM document_files WHERE document_id = $1::uuid",
                document_id,
            )
        logger.info("docx cache invalidated for %s", document_id)
    except Exception as e:
        logger.debug("docx cache invalidate failed: %s", e)
