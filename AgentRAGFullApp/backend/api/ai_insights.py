"""Sprint 9 · AI Insights API · sugerencias proactivas del agente."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/insights", tags=["ai_insights"])


def _serialize(r) -> dict:
    return {
        "id": str(r["id"]),
        "kind": r["kind"],
        "severity": r["severity"],
        "target_type": r["target_type"],
        "target_id": str(r["target_id"]) if r["target_id"] else None,
        "title": r["title"],
        "body": r["body"],
        "suggested_action": r["suggested_action"],
        "action_payload": r["action_payload"],
        "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
        "status": r["status"],
        "accepted_at": r["accepted_at"].isoformat() if r["accepted_at"] else None,
        "dismissed_at": r["dismissed_at"].isoformat() if r["dismissed_at"] else None,
        "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
        "generated_by": r["generated_by"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


@router.get("")
async def list_insights(
    status: Optional[str] = Query(default="new", regex="^(new|accepted|dismissed|expired)$"),
    severity: Optional[str] = Query(default=None, regex="^(info|warning|critical)$"),
    target_type: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    where = ["firm_id = $1::uuid"]
    params: list = [principal.firm_id]
    if status:
        params.append(status); where.append(f"status = ${len(params)}")
    if severity:
        params.append(severity); where.append(f"severity = ${len(params)}")
    if target_type:
        params.append(target_type); where.append(f"target_type = ${len(params)}")
    params.append(limit)
    sql = f"""
        select id, kind, severity, target_type, target_id, title, body,
               suggested_action, action_payload, confidence, status,
               accepted_at, dismissed_at, expires_at, generated_by, created_at
          from ai_insights
         where {' and '.join(where)}
         order by case severity when 'critical' then 1 when 'warning' then 2 else 3 end,
                  confidence desc, created_at desc
         limit ${len(params)}
    """
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {"count": len(rows), "items": [_serialize(r) for r in rows]}


@router.get("/counts")
async def counts(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select
              (select count(*) from ai_insights where firm_id = $1::uuid and status = 'new') as new,
              (select count(*) from ai_insights where firm_id = $1::uuid and status = 'new' and severity = 'critical') as critical,
              (select count(*) from ai_insights where firm_id = $1::uuid and status = 'new' and severity = 'warning') as warning,
              (select count(*) from ai_insights where firm_id = $1::uuid and status = 'accepted') as accepted_total
            """,
            principal.firm_id,
        )
    return {
        "new": row["new"] or 0,
        "critical": row["critical"] or 0,
        "warning": row["warning"] or 0,
        "accepted_total": row["accepted_total"] or 0,
    }


@router.post("/{insight_id}/accept")
async def accept(insight_id: str, principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update ai_insights set status = 'accepted', accepted_at = now()
             where id = $1::uuid and firm_id = $2::uuid and status = 'new'
            returning id, status, action_payload
            """,
            insight_id, principal.firm_id,
        )
    if not row:
        raise HTTPException(409, "no se puede aceptar (no existe o ya procesado)")
    return {"id": str(row["id"]), "status": row["status"], "action_payload": row["action_payload"]}


@router.post("/{insight_id}/dismiss")
async def dismiss(insight_id: str, principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update ai_insights set status = 'dismissed', dismissed_at = now()
             where id = $1::uuid and firm_id = $2::uuid and status = 'new'
            returning id
            """,
            insight_id, principal.firm_id,
        )
    if not row:
        raise HTTPException(404, "not found")
    return {"id": str(row["id"]), "status": "dismissed"}


@router.post("/dismiss-all")
async def dismiss_all(
    severity: Optional[str] = Query(default=None, regex="^(info|warning|critical)$"),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    where = "firm_id = $1::uuid and status = 'new'"
    params: list = [principal.firm_id]
    if severity:
        params.append(severity); where += f" and severity = ${len(params)}"
    async with storage.pool.acquire() as conn:
        n = await conn.fetchval(
            f"with d as (update ai_insights set status = 'dismissed', dismissed_at = now() where {where} returning 1) select count(*) from d",
            *params,
        )
    return {"dismissed": n or 0}


@router.post("/refresh")
async def refresh(principal: Principal = Depends(get_current_firm)):
    """Dispara el generador. Idempotente."""
    if principal.role not in ("admin", "socio_senior", "socio_junior", "lawyer"):
        raise HTTPException(403, "Tu rol no puede regenerar insights")
    from agent.workers.insight_generator import generate_for_firm
    return await generate_for_firm(str(principal.firm_id))


# ════════════════════════════════════════════════════════════════════════
# Voice tool
# ════════════════════════════════════════════════════════════════════════


async def generate_insights_tool(args: dict, ctx: dict) -> dict:
    """Voice: 'LexAI, ¿qué debería hacer hoy?'."""
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    from agent.workers.insight_generator import generate_for_firm
    result = await generate_for_firm(firm_id)
    # Top 5 después de generar
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return result
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select kind, severity, title, body, suggested_action, confidence
              from ai_insights where firm_id = $1::uuid and status = 'new'
             order by case severity when 'critical' then 1 when 'warning' then 2 else 3 end,
                      confidence desc
             limit 5
            """,
            firm_id,
        )
    return {
        **result,
        "top_5": [
            {
                "kind": r["kind"], "severity": r["severity"],
                "title": r["title"], "body": r["body"],
                "suggested_action": r["suggested_action"],
                "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
            }
            for r in rows
        ],
    }
