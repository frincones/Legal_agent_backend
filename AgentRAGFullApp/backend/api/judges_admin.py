"""Sprint 20 · Judges admin endpoints.

  POST /v1/judges-admin/reindex-now?limit=N    · admin · corre worker

(En el futuro: importar más jueces desde un CSV / endpoint público.)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/judges-admin", tags=["judges_admin"])


@router.post("/reindex-now")
async def reindex_now(
    limit: int = Query(default=50, ge=1, le=500),
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin / socio_senior")
    from agent.workers.judge_profile_indexer import reindex_judges
    return await reindex_judges(limit=limit)
