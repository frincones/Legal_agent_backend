"""Sprint M21.S8 · Admin Hardening API.

Endpoints (firm-scoped):
  GET    /v2/admin/usage                     · uso del mes actual (counters)
  GET    /v2/admin/usage/{month}             · uso de un mes especifico (YYYY-MM)
  GET    /v2/admin/audit                     · ultimos audit events del firm
  GET    /v2/admin/rate-limits               · contadores actuales (last hour)
  POST   /v2/admin/habeas-data/request       · solicitar export (Ley 1581)
  GET    /v2/admin/habeas-data               · listar exports del firm
  GET    /v2/admin/habeas-data/{export_id}   · descarga JSON del export
  GET    /v2/admin/metrics                   · metrics agregados (Prometheus-style text)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm
from utils.db import get_storage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v2/admin", tags=["admin-hardening"])


@router.get("/usage")
async def usage_current(principal: Principal = Depends(get_current_firm)):
    from lex.hardening.usage import get_usage_summary
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    return await get_usage_summary(pool, UUID(str(principal.firm_id)))


@router.get("/usage/{month}")
async def usage_by_month(month: str, principal: Principal = Depends(get_current_firm)):
    from lex.hardening.usage import get_usage_summary
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    try:
        datetime.strptime(month, "%Y-%m")
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "month formato esperado YYYY-MM")
    return await get_usage_summary(pool, UUID(str(principal.firm_id)), period_month=month)


@router.get("/audit")
async def list_audit(
    principal: Principal = Depends(get_current_firm),
    limit: int = Query(100, le=500),
    event_kind: Optional[str] = None,
):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    where = ["firm_id = $1::uuid"]
    args: list = [str(principal.firm_id)]
    if event_kind:
        args.append(event_kind)
        where.append(f"event_kind = ${len(args)}")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            select event_id, event_kind, actor_user_id, actor_role,
                   ip_address::text, target_resource, summary, details, created_at
              from admin_audit_events
             where {' and '.join(where)}
             order by created_at desc limit {int(limit)}
            """,
            *args,
        )
    return {
        "items": [
            {
                "event_id": int(r["event_id"]),
                "event_kind": r["event_kind"],
                "actor_user_id": str(r["actor_user_id"]) if r["actor_user_id"] else None,
                "actor_role": r["actor_role"],
                "ip_address": r["ip_address"],
                "target_resource": r["target_resource"],
                "summary": r["summary"],
                "details": dict(r["details"] or {}),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.get("/rate-limits")
async def current_rate_limits(principal: Principal = Depends(get_current_firm)):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select resource_type,
                   sum(count) filter (where window_start > now() - interval '1 minute') as count_minute,
                   sum(count) filter (where window_start > now() - interval '1 hour') as count_hour,
                   max(last_request_at) as last_at
              from rate_limit_buckets
             where firm_id = $1::uuid
               and window_start > now() - interval '1 hour'
             group by resource_type
             order by resource_type
            """,
            str(principal.firm_id),
        )
    from lex.hardening.rate_limit import DEFAULT_LIMITS
    items = []
    for r in rows:
        rt = r["resource_type"]
        lim = DEFAULT_LIMITS.get(rt, {"per_minute": 60, "per_hour": 1000})
        items.append({
            "resource_type": rt,
            "count_minute": int(r["count_minute"] or 0),
            "count_hour": int(r["count_hour"] or 0),
            "limit_minute": lim["per_minute"],
            "limit_hour": lim["per_hour"],
            "last_at": r["last_at"].isoformat() if r["last_at"] else None,
        })
    return {"items": items}


class HabeasRequestBody(BaseModel):
    subject_kind: str = Field(pattern="^(cedula|email|client_id)$")
    subject_id: str = Field(min_length=1, max_length=200)


@router.post("/habeas-data/request")
async def habeas_request(
    body: HabeasRequestBody,
    request: Request,
    principal: Principal = Depends(get_current_firm),
):
    """Crea export Habeas Data. Procesa SINCRONO (datos chicos), guarda JSON in-DB."""
    from lex.hardening.habeas_data import collect_subject_data
    from lex.hardening.usage import admin_audit, record_usage
    from lex.hardening.rate_limit import check_and_consume

    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")

    # Rate limit (max 2/min, 10/hr)
    rl = await check_and_consume(pool, firm_id=UUID(str(principal.firm_id)), resource_type="habeas_export")
    if not rl.get("permitted"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, rl.get("reason") or "rate_limited")

    # Create export row
    export_id = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            insert into habeas_data_exports
                (export_id, firm_id, subject_id, subject_kind,
                 requested_by_user_id, status)
            values ($1::uuid, $2::uuid, $3, $4, $5, 'processing')
            """,
            str(export_id), str(principal.firm_id),
            body.subject_id, body.subject_kind,
            str(principal.user_id) if principal.user_id else None,
        )

    # Process inline (synchronous - data tipicamente <100KB)
    try:
        data = await collect_subject_data(
            pool, firm_id=UUID(str(principal.firm_id)),
            subject_kind=body.subject_kind, subject_id=body.subject_id,
        )
        payload = json.dumps(data, ensure_ascii=False, default=str)
        async with pool.acquire() as conn:
            await conn.execute(
                """
                update habeas_data_exports
                   set status='ready', completed_at=now(),
                       file_size_bytes=$1,
                       metadata=$2::jsonb,
                       tables_included=$3::jsonb,
                       expires_at=now() + interval '7 days'
                 where export_id=$4::uuid
                """,
                len(payload.encode("utf-8")),
                json.dumps({"_inline_payload": payload}, ensure_ascii=False),
                json.dumps(data.get("tables_included", []), ensure_ascii=False),
                str(export_id),
            )
        await record_usage(pool, firm_id=UUID(str(principal.firm_id)), resource_type="habeas_export")
        await admin_audit(
            pool, event_kind="habeas_data_request",
            firm_id=UUID(str(principal.firm_id)),
            actor_user_id=UUID(str(principal.user_id)) if principal.user_id else None,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            target_resource=f"{body.subject_kind}:{body.subject_id}",
            summary=f"Habeas Data export creado · {len(data.get('items') or {})} tablas",
            details={"export_id": str(export_id)},
        )
        return {
            "export_id": str(export_id),
            "status": "ready",
            "tables_included": data.get("tables_included", []),
            "tables_skipped": data.get("tables_skipped", []),
            "subject_kind": body.subject_kind,
            "subject_id": body.subject_id,
            "size_bytes": len(payload.encode("utf-8")),
        }
    except Exception as e:
        logger.exception("habeas_request failed")
        async with pool.acquire() as conn:
            await conn.execute(
                "update habeas_data_exports set status='failed', error_message=$1, completed_at=now() where export_id=$2::uuid",
                str(e)[:500], str(export_id),
            )
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(e))


@router.get("/habeas-data")
async def list_habeas_exports(
    principal: Principal = Depends(get_current_firm),
    limit: int = Query(50, le=200),
):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select export_id, subject_id, subject_kind, status,
                   requested_at, completed_at, file_size_bytes, expires_at,
                   tables_included, error_message
              from habeas_data_exports
             where firm_id=$1::uuid
             order by requested_at desc limit $2
            """,
            str(principal.firm_id), int(limit),
        )
    return {
        "items": [
            {
                "export_id": str(r["export_id"]),
                "subject_kind": r["subject_kind"], "subject_id": r["subject_id"],
                "status": r["status"],
                "requested_at": r["requested_at"].isoformat() if r["requested_at"] else None,
                "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
                "file_size_bytes": r["file_size_bytes"],
                "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
                "tables_included": list(r["tables_included"] or []),
                "error_message": r["error_message"],
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.get("/habeas-data/{export_id}")
async def download_habeas_export(export_id: str, principal: Principal = Depends(get_current_firm)):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select status, metadata, expires_at
              from habeas_data_exports
             where export_id=$1::uuid and firm_id=$2::uuid
            """,
            export_id, str(principal.firm_id),
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "export no encontrado")
    if row["status"] != "ready":
        raise HTTPException(status.HTTP_409_CONFLICT, f"export status={row['status']}, no descargable")
    if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_410_GONE, "export expirado")
    payload = (row["metadata"] or {}).get("_inline_payload") if isinstance(row["metadata"], dict) else None
    if not payload:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "payload inline no encontrado")
    return Response(
        content=payload,
        media_type="application/json",
        headers={
            "content-disposition": f'attachment; filename="habeas_data_{export_id}.json"',
        },
    )


@router.get("/metrics")
async def prometheus_metrics(principal: Principal = Depends(get_current_firm)):
    """Metrics agregados en formato Prometheus-style text plain."""
    from lex.hardening.usage import get_usage_summary
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    summary = await get_usage_summary(pool, UUID(str(principal.firm_id)))
    lines = [
        "# HELP lexai_firm_usage_count Resource usage count this month",
        "# TYPE lexai_firm_usage_count gauge",
    ]
    for item in summary["items"]:
        lines.append(
            f'lexai_firm_usage_count{{firm_id="{principal.firm_id}",resource="{item["resource_type"]}",month="{summary["period_month"]}"}} {item["count"]}'
        )
    lines.append("# HELP lexai_firm_usage_cost_usd Cost USD this month")
    lines.append("# TYPE lexai_firm_usage_cost_usd gauge")
    for item in summary["items"]:
        lines.append(
            f'lexai_firm_usage_cost_usd{{firm_id="{principal.firm_id}",resource="{item["resource_type"]}",month="{summary["period_month"]}"}} {item["cost_usd"]}'
        )
    return Response(content="\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
