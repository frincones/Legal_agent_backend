"""Sprint 15 · KB admin endpoints.

  POST /v1/kb/reindex-now    · admin / socio_senior — invoca worker kb_indexer
  GET  /v1/kb/health         · health del subsistema KB (embed disponible?)

Lo separamos del router /v1/kb (knowledge_base.py) para que las
operaciones administrativas tengan su propio espacio y control de
permisos sin contaminar el CRUD de uso diario.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Query

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/kb-admin", tags=["kb_admin"])


@router.get("/health")
async def kb_health(_: Principal = Depends(get_current_firm)):
    """Estado del subsistema KB (sin exponer secretos)."""
    has_openai = bool(os.getenv("OPENAI_API_KEY"))
    from utils.db import get_storage
    storage = await get_storage()
    has_pool = hasattr(storage, "pool")
    has_vector_ext = False
    if has_pool:
        try:
            async with storage.pool.acquire() as conn:
                has_vector_ext = bool(
                    await conn.fetchval(
                        "select exists(select 1 from pg_extension where extname='vector')"
                    )
                )
        except Exception as e:
            logger.debug("kb_health vector check failed: %s", e)
    return {
        "openai_configured": has_openai,
        "storage_pool": has_pool,
        "pgvector_extension": has_vector_ext,
        "ready": has_openai and has_pool and has_vector_ext,
    }


@router.post("/reindex-now")
async def reindex_now(
    entries_limit: int = Query(default=32, ge=1, le=200),
    lessons_limit: int = Query(default=32, ge=1, le=200),
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior", "socio_junior"):
        raise HTTPException(403, "Sin permisos · solo socios/admin")
    from agent.workers.kb_indexer import run_full_pass
    return await run_full_pass(entries_limit=entries_limit, lessons_limit=lessons_limit)
