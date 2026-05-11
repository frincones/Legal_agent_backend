"""Sprint 6 · Quotas API.

Endpoints:
  GET  /v1/quotas/current               · plan + uso del periodo + warnings
  GET  /v1/quotas/check?kind=llm_call   · ¿puedo hacer una llamada más?
  POST /v1/quotas/track                 · escritura manual de usage_event (debugging)

La fuente de verdad es la RPC `lexai_firm_usage(firm_id)` que une
firm_subscriptions + subscription_plans + usage_events del periodo.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/quotas", tags=["quotas"])


def _quota_key_for(kind: str) -> Optional[str]:
    return {
        "llm_call": "llm_calls_mo",
        "voice_minute": "voice_min_mo",
        "document_upload": "documents_mo",
        "email_sync": "email_accounts",
        "judicial_poll": "judicial_subs",
        "canvas_generate": "llm_calls_mo",
    }.get(kind)


@router.get("/current")
async def current(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        data = await conn.fetchval("select lexai_firm_usage($1::uuid)", principal.firm_id)

    data = data or {}
    quotas = data.get("quotas") or {}
    usage = data.get("usage") or {}
    warnings: list[dict] = []
    for kind, used in usage.items():
        qkey = _quota_key_for(kind)
        if not qkey:
            continue
        limit = quotas.get(qkey)
        if limit is None:
            continue  # unlimited
        try:
            ratio = (used or 0) / max(limit, 1)
        except Exception:
            ratio = 0
        if ratio >= 1.0:
            warnings.append({"kind": kind, "used": used, "limit": limit, "level": "exceeded"})
        elif ratio >= 0.8:
            warnings.append({"kind": kind, "used": used, "limit": limit, "level": "warning"})
    data["warnings"] = warnings
    return data


@router.get("/check")
async def check(
    kind: str = Query(min_length=1),
    cost: int = Query(default=1, ge=1, le=1000),
    principal: Principal = Depends(get_current_firm),
):
    qkey = _quota_key_for(kind)
    if not qkey:
        return {"allowed": True, "reason": "unmetered_kind"}
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        data = await conn.fetchval("select lexai_firm_usage($1::uuid)", principal.firm_id)
    data = data or {}
    quotas = data.get("quotas") or {}
    usage = data.get("usage") or {}
    limit = quotas.get(qkey)
    if limit is None:
        return {"allowed": True, "limit": None, "used": usage.get(kind, 0)}
    used = usage.get(kind, 0) or 0
    allowed = (used + cost) <= limit
    return {"allowed": allowed, "limit": limit, "used": used, "would_be": used + cost}


class TrackRequest(BaseModel):
    kind: str = Field(min_length=1)
    count: int = Field(default=1, ge=1)
    cost_units: float = 0
    metadata: Optional[dict] = None


@router.post("/track")
async def track(
    body: TrackRequest,
    principal: Principal = Depends(get_current_firm),
):
    from utils.audit import track_usage
    await track_usage(
        firm_id=str(principal.firm_id),
        user_id=str(principal.user_id),
        kind=body.kind,
        count=body.count,
        cost_units=body.cost_units,
        metadata=body.metadata,
    )
    return {"ok": True}


# ════════════════════════════════════════════════════════════════════════
# Voice tool
# ════════════════════════════════════════════════════════════════════════


async def check_quota_tool(args: dict, ctx: dict) -> dict:
    """Voice tool: 'LexAI, ¿cuánto me queda en el plan?'."""
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}
    async with storage.pool.acquire() as conn:
        data = await conn.fetchval("select lexai_firm_usage($1::uuid)", firm_id)
    return data or {}
