"""Sprint 8 · Time entries API.

  GET  /v1/time-entries?matter_id=&user_id=&since=&until=&limit=
  GET  /v1/time-entries/running                 · entry en curso del usuario
  GET  /v1/time-entries/summary?matter_id=...   · totales del periodo (RPC)
  POST /v1/time-entries                         · crear entry manual
  POST /v1/time-entries/start                   · arrancar timer (cierra cualquier activo)
  POST /v1/time-entries/{id}/stop               · detener timer
  PATCH /v1/time-entries/{id}                   · editar (no facturadas)
  DELETE /v1/time-entries/{id}                  · soft delete (no facturadas)
  GET   /v1/time-entries/rates?matter_id=...    · tarifas vigentes
  POST  /v1/time-entries/rates                  · setear tarifa
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/time-entries", tags=["time_entries"])


def _serialize(r) -> dict:
    return {
        "id": str(r["id"]),
        "matter_id": str(r["matter_id"]),
        "user_id": str(r["user_id"]),
        "started_at": r["started_at"].isoformat() if r["started_at"] else None,
        "ended_at": r["ended_at"].isoformat() if r["ended_at"] else None,
        "duration_min": r["duration_min"],
        "billable": r["billable"],
        "rate_cop": float(r["rate_cop"]) if r["rate_cop"] is not None else None,
        "description": r["description"],
        "source": r["source"],
        "invoiced": r["invoice_line_id"] is not None,
    }


@router.get("")
async def list_entries(
    matter_id: Optional[str] = None,
    user_id: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    billable: Optional[bool] = None,
    only_unbilled: bool = False,
    limit: int = Query(default=100, le=500),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    where = ["firm_id = $1::uuid"]
    params: list = [principal.firm_id]
    if matter_id:
        params.append(matter_id); where.append(f"matter_id = ${len(params)}::uuid")
    if user_id:
        params.append(user_id); where.append(f"user_id = ${len(params)}::uuid")
    if since:
        params.append(since); where.append(f"started_at >= ${len(params)}::timestamptz")
    if until:
        params.append(until); where.append(f"started_at <= ${len(params)}::timestamptz")
    if billable is not None:
        params.append(billable); where.append(f"billable = ${len(params)}")
    if only_unbilled:
        where.append("invoice_line_id is null")
    params.append(limit)
    sql = f"""
        select id, matter_id, user_id, started_at, ended_at, duration_min,
               billable, rate_cop, description, source, invoice_line_id
          from time_entries
         where {' and '.join(where)}
         order by started_at desc
         limit ${len(params)}
    """
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {"count": len(rows), "items": [_serialize(r) for r in rows]}


@router.get("/running")
async def get_running(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, matter_id, user_id, started_at, ended_at, duration_min,
                   billable, rate_cop, description, source, invoice_line_id
              from time_entries
             where firm_id = $1::uuid and user_id = $2::uuid and ended_at is null
             order by started_at desc limit 1
            """,
            principal.firm_id, principal.user_id,
        )
    return {"running": _serialize(row) if row else None}


@router.get("/summary")
async def billable_summary(
    matter_id: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        data = await conn.fetchval(
            "select lexai_billable_summary($1::uuid, $2::uuid, $3::date, $4::date)",
            principal.firm_id, matter_id, since, until,
        )
    return data or {}


class CreateRequest(BaseModel):
    matter_id: str
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    duration_min: Optional[int] = Field(default=None, ge=1, le=24 * 60)
    description: str = ""
    billable: bool = True
    rate_cop: Optional[float] = None
    source: str = Field(default="manual", pattern="^(manual|timer|voice)$")


@router.post("")
async def create_entry(
    body: CreateRequest,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")

    # Si solo dan duration_min, computamos started/ended desde now.
    started = body.started_at
    ended = body.ended_at
    if not started and body.duration_min:
        from datetime import datetime, timedelta, timezone
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=body.duration_min)
        started = start.isoformat()
        ended = end.isoformat()

    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into time_entries
              (firm_id, matter_id, user_id, started_at, ended_at,
               billable, rate_cop, description, source)
            values ($1::uuid, $2::uuid, $3::uuid, $4::timestamptz, $5::timestamptz,
                    $6, $7, $8, $9)
            returning id, matter_id, user_id, started_at, ended_at, duration_min,
                      billable, rate_cop, description, source, invoice_line_id
            """,
            principal.firm_id, body.matter_id, principal.user_id,
            started, ended, body.billable, body.rate_cop, body.description, body.source,
        )
    return _serialize(row)


class StartRequest(BaseModel):
    matter_id: str
    description: str = ""


@router.post("/start")
async def start_timer(
    body: StartRequest,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        # Cerrar cualquier timer activo del usuario
        await conn.execute(
            """
            update time_entries set ended_at = now()
             where firm_id = $1::uuid and user_id = $2::uuid and ended_at is null
            """,
            principal.firm_id, principal.user_id,
        )
        row = await conn.fetchrow(
            """
            insert into time_entries
              (firm_id, matter_id, user_id, started_at, description, source)
            values ($1::uuid, $2::uuid, $3::uuid, now(), $4, 'timer')
            returning id, matter_id, user_id, started_at, ended_at, duration_min,
                      billable, rate_cop, description, source, invoice_line_id
            """,
            principal.firm_id, body.matter_id, principal.user_id, body.description,
        )
    return _serialize(row)


@router.post("/{entry_id}/stop")
async def stop_timer(
    entry_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update time_entries set ended_at = now()
             where id = $1::uuid and firm_id = $2::uuid and ended_at is null
             returning id, matter_id, user_id, started_at, ended_at, duration_min,
                       billable, rate_cop, description, source, invoice_line_id
            """,
            entry_id, principal.firm_id,
        )
    if not row:
        raise HTTPException(404, "no running entry")
    return _serialize(row)


class PatchRequest(BaseModel):
    description: Optional[str] = None
    billable: Optional[bool] = None
    rate_cop: Optional[float] = None
    duration_min: Optional[int] = Field(default=None, ge=1, le=24 * 60)


@router.patch("/{entry_id}")
async def patch_entry(
    entry_id: str,
    body: PatchRequest,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    fields, params = [], [entry_id, principal.firm_id]
    if body.description is not None:
        params.append(body.description); fields.append(f"description = ${len(params)}")
    if body.billable is not None:
        params.append(body.billable); fields.append(f"billable = ${len(params)}")
    if body.rate_cop is not None:
        params.append(body.rate_cop); fields.append(f"rate_cop = ${len(params)}")
    if body.duration_min is not None:
        # Ajustar ended_at relativo al started_at
        params.append(body.duration_min)
        fields.append(f"ended_at = started_at + (${len(params)}::int || ' minutes')::interval")
    if not fields:
        raise HTTPException(400, "nada que actualizar")
    sql = f"""
        update time_entries set {', '.join(fields)}, updated_at = now()
         where id = $1::uuid and firm_id = $2::uuid and invoice_line_id is null
         returning id, matter_id, user_id, started_at, ended_at, duration_min,
                   billable, rate_cop, description, source, invoice_line_id
    """
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(sql, *params)
    if not row:
        raise HTTPException(404, "no encontrado o ya facturado")
    return _serialize(row)


@router.delete("/{entry_id}")
async def delete_entry(
    entry_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        n = await conn.execute(
            """
            delete from time_entries
             where id = $1::uuid and firm_id = $2::uuid and invoice_line_id is null
            """,
            entry_id, principal.firm_id,
        )
    return {"deleted": n}


# ──────────────────────────────────────────────────────────────────────
# Hourly rates
# ──────────────────────────────────────────────────────────────────────


class RateRequest(BaseModel):
    matter_id: str
    user_id: Optional[str] = None
    rate_cop: float = Field(ge=0)
    effective_from: Optional[str] = None


@router.get("/rates")
async def list_rates(
    matter_id: str = Query(...),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, user_id, rate_cop, effective_from, effective_to
              from matter_hourly_rates
             where firm_id = $1::uuid and matter_id = $2::uuid
             order by effective_from desc
            """,
            principal.firm_id, matter_id,
        )
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]),
                "user_id": str(r["user_id"]) if r["user_id"] else None,
                "rate_cop": float(r["rate_cop"]),
                "effective_from": r["effective_from"].isoformat() if r["effective_from"] else None,
                "effective_to": r["effective_to"].isoformat() if r["effective_to"] else None,
            }
            for r in rows
        ],
    }


@router.post("/rates")
async def set_rate(
    body: RateRequest,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior", "socio_junior"):
        raise HTTPException(403, "Solo socios/admin pueden setear tarifas")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into matter_hourly_rates
              (firm_id, matter_id, user_id, rate_cop, effective_from)
            values ($1::uuid, $2::uuid, $3::uuid, $4, coalesce($5::date, current_date))
            returning id, rate_cop, effective_from
            """,
            principal.firm_id, body.matter_id, body.user_id, body.rate_cop, body.effective_from,
        )
    return {"id": str(row["id"]), "rate_cop": float(row["rate_cop"])}


# ════════════════════════════════════════════════════════════════════════
# Voice tool
# ════════════════════════════════════════════════════════════════════════


async def track_time_tool(args: dict, ctx: dict) -> dict:
    """Voice: 'LexAI, registra 45 minutos en el caso de Avianca trabajando en la contestación'."""
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    minutes = int(args.get("minutes") or 0)
    description = (args.get("description") or "").strip()
    if not (firm_id and user_id and matter_id and minutes > 0):
        return {"error": "firm_id, user_id, matter_id, minutes>0 requeridos"}
    from utils.db import get_storage
    from datetime import datetime, timedelta, timezone
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into time_entries
              (firm_id, matter_id, user_id, started_at, ended_at,
               billable, description, source)
            values ($1::uuid, $2::uuid, $3::uuid, $4::timestamptz, $5::timestamptz,
                    true, $6, 'voice')
            returning id, duration_min
            """,
            firm_id, matter_id, user_id, start, end, description,
        )
    return {"id": str(row["id"]), "duration_min": row["duration_min"], "matter_id": matter_id}
