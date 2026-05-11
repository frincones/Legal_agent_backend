"""Sprint 6 · Audit log API (compliance Habeas Data CO).

Endpoints:
  GET  /v1/audit/logs                         · filter por user/action/resource/date
  GET  /v1/audit/counts                       · counts agrupados (admin dashboard)
  GET  /v1/audit/habeas-data/{subject_id}     · export por titular de dato (NIT/CC)
  POST /v1/audit/log                          · escritura manual (raro, para sistemas externos)

Permisos:
  · select: admin + socio_senior + socio_junior
  · export: admin
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/audit", tags=["audit"])

ADMIN_ROLES = {"admin", "socio_senior", "socio_junior"}


def _ensure_admin(p: Principal):
    if p.role not in ADMIN_ROLES:
        raise HTTPException(403, f"Solo socios/admin pueden ver auditoría. Tu rol: {p.role}")


@router.get("/logs")
async def list_logs(
    user_id: Optional[str] = None,
    action: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    data_subject_id: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    outcome: Optional[str] = Query(default=None, regex="^(success|denied|error)$"),
    limit: int = Query(default=100, le=1000),
    principal: Principal = Depends(get_current_firm),
):
    _ensure_admin(principal)
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")

    where = ["firm_id = $1::uuid"]
    params: list = [principal.firm_id]
    if user_id:
        params.append(user_id); where.append(f"user_id = ${len(params)}::uuid")
    if action:
        params.append(action); where.append(f"action = ${len(params)}")
    if resource_type:
        params.append(resource_type); where.append(f"resource_type = ${len(params)}")
    if resource_id:
        params.append(resource_id); where.append(f"resource_id = ${len(params)}")
    if data_subject_id:
        params.append(data_subject_id); where.append(f"data_subject_id = ${len(params)}")
    if outcome:
        params.append(outcome); where.append(f"outcome = ${len(params)}")
    if since:
        params.append(since); where.append(f"occurred_at >= ${len(params)}::timestamptz")
    if until:
        params.append(until); where.append(f"occurred_at <= ${len(params)}::timestamptz")
    params.append(limit)
    sql = f"""
        select id, user_id, action, resource_type, resource_id,
               ip_address::text as ip_address, user_agent, outcome, reason,
               data_subject_id, metadata, occurred_at
          from audit_logs
         where {' and '.join(where)}
         order by occurred_at desc
         limit ${len(params)}
    """
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {
        "count": len(rows),
        "items": [
            {
                "id": r["id"],
                "user_id": str(r["user_id"]) if r["user_id"] else None,
                "action": r["action"],
                "resource_type": r["resource_type"],
                "resource_id": r["resource_id"],
                "ip_address": r["ip_address"],
                "user_agent": r["user_agent"],
                "outcome": r["outcome"],
                "reason": r["reason"],
                "data_subject_id": r["data_subject_id"],
                "metadata": r["metadata"],
                "occurred_at": r["occurred_at"].isoformat() if r["occurred_at"] else None,
            }
            for r in rows
        ],
    }


@router.get("/counts")
async def audit_counts(
    days: int = Query(default=7, le=90),
    principal: Principal = Depends(get_current_firm),
):
    _ensure_admin(principal)
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        total = await conn.fetchval(
            """
            select count(*) from audit_logs
             where firm_id = $1::uuid and occurred_at >= now() - ($2::int || ' days')::interval
            """,
            principal.firm_id, days,
        )
        by_action = await conn.fetch(
            """
            select action, count(*) as n
              from audit_logs
             where firm_id = $1::uuid and occurred_at >= now() - ($2::int || ' days')::interval
             group by action order by n desc limit 20
            """,
            principal.firm_id, days,
        )
        denied = await conn.fetchval(
            """
            select count(*) from audit_logs
             where firm_id = $1::uuid and outcome = 'denied'
               and occurred_at >= now() - ($2::int || ' days')::interval
            """,
            principal.firm_id, days,
        )
    return {
        "total": total or 0,
        "denied": denied or 0,
        "by_action": [{"action": r["action"], "count": r["n"]} for r in by_action],
        "days": days,
    }


@router.get("/habeas-data/{subject_id}")
async def habeas_data_export(
    subject_id: str,
    format: str = Query(default="json", regex="^(json|csv)$"),
    principal: Principal = Depends(get_current_firm),
):
    """Export de Habeas Data · Ley 1581/2012 CO Art. 14.

    Devuelve TODOS los accesos a la información del titular cuya identificación
    sea `subject_id` (NIT/CC). Útil cuando el titular ejerce el derecho a saber
    qué datos suyos se manejan.
    """
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin/socio_senior puede exportar habeas data")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select occurred_at, action, resource_type, resource_id,
                   user_id, ip_address::text as ip_address, outcome
              from audit_logs
             where firm_id = $1::uuid and data_subject_id = $2
             order by occurred_at desc
            """,
            principal.firm_id, subject_id,
        )
    if format == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["occurred_at", "action", "resource_type", "resource_id", "user_id", "ip", "outcome"])
        for r in rows:
            w.writerow([
                r["occurred_at"].isoformat() if r["occurred_at"] else "",
                r["action"], r["resource_type"] or "", r["resource_id"] or "",
                str(r["user_id"]) if r["user_id"] else "",
                r["ip_address"] or "", r["outcome"],
            ])
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={
                "content-disposition": f'attachment; filename="habeas_data_{subject_id}.csv"'
            },
        )
    return {
        "subject_id": subject_id,
        "count": len(rows),
        "items": [
            {
                "occurred_at": r["occurred_at"].isoformat() if r["occurred_at"] else None,
                "action": r["action"],
                "resource_type": r["resource_type"],
                "resource_id": r["resource_id"],
                "user_id": str(r["user_id"]) if r["user_id"] else None,
                "ip_address": r["ip_address"],
                "outcome": r["outcome"],
            }
            for r in rows
        ],
    }


class ManualLogRequest(BaseModel):
    action: str
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    data_subject_id: Optional[str] = None
    outcome: str = Field(default="success", pattern="^(success|denied|error)$")
    reason: Optional[str] = None
    metadata: Optional[dict] = None


@router.post("/log")
async def manual_log(
    body: ManualLogRequest,
    request: Request,
    principal: Principal = Depends(get_current_firm),
):
    from utils.audit import audit_log, audit_from_request
    extra = audit_from_request(request)
    await audit_log(
        firm_id=str(principal.firm_id),
        user_id=str(principal.user_id),
        action=body.action,
        resource_type=body.resource_type,
        resource_id=body.resource_id,
        data_subject_id=body.data_subject_id,
        outcome=body.outcome,
        reason=body.reason,
        metadata=body.metadata,
        **extra,
    )
    return {"ok": True}


# ════════════════════════════════════════════════════════════════════════
# Voice tool
# ════════════════════════════════════════════════════════════════════════


async def query_audit_logs_tool(args: dict, ctx: dict) -> dict:
    """Voice tool: 'LexAI, ¿quién accedió al cliente X?'."""
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    role = ctx.get("role", "")
    if role not in ADMIN_ROLES:
        return {"error": "solo socios/admin pueden consultar auditoría"}
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}
    where = ["firm_id = $1::uuid"]
    params = [firm_id]
    if args.get("data_subject_id"):
        params.append(args["data_subject_id"]); where.append(f"data_subject_id = ${len(params)}")
    if args.get("resource_id"):
        params.append(args["resource_id"]); where.append(f"resource_id = ${len(params)}")
    if args.get("action"):
        params.append(args["action"]); where.append(f"action = ${len(params)}")
    limit = min(int(args.get("limit") or 10), 50)
    params.append(limit)
    sql = f"""
        select user_id, action, resource_type, resource_id, occurred_at, ip_address::text as ip
          from audit_logs
         where {' and '.join(where)}
         order by occurred_at desc
         limit ${len(params)}
    """
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {
        "count": len(rows),
        "items": [
            {
                "user_id": str(r["user_id"]) if r["user_id"] else None,
                "action": r["action"],
                "resource": f"{r['resource_type']}/{r['resource_id']}" if r["resource_type"] else None,
                "ip": r["ip"],
                "when": r["occurred_at"].isoformat() if r["occurred_at"] else None,
            }
            for r in rows
        ],
    }
