"""Sprint 18 · Saved reports API.

  GET    /v2/reports?scope=...                · list reports del firm + own
  GET    /v2/reports/{id}
  POST   /v2/reports                          · crear
  PATCH  /v2/reports/{id}
  DELETE /v2/reports/{id}
  POST   /v2/reports/{id}/run                 · ejecuta y guarda en report_runs
  GET    /v2/reports/{id}/runs?limit=         · historial de ejecuciones

config esperado en `config` jsonb:
  {
    "metrics": ["billable_minutes", "invoiced_cop"],
    "group_by": "user" | "matter" | "materia" | "month",
    "filters": {"materia": "civil", "status": "activo"},
    "period_days": 30
  }

`scope` determina qué RPC subyacente se llama. Esto mantiene los reports
acotados a queries que sabemos son seguras (no SQL libre).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v2/reports", tags=["reports"])


VALID_SCOPES = {"revenue", "performance", "pipeline", "predictions", "matters", "time", "custom"}


class ReportIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = None
    scope: str
    config: dict = Field(default_factory=dict)
    shared_with_firm: bool = False
    pinned: bool = False


class ReportPatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    config: Optional[dict] = None
    shared_with_firm: Optional[bool] = None
    pinned: Optional[bool] = None


def _serialize(r) -> dict:
    return {
        "id": str(r["id"]),
        "name": r["name"],
        "description": r["description"],
        "scope": r["scope"],
        "config": r["config"] if not isinstance(r["config"], str) else (json.loads(r["config"]) if r["config"] else {}),
        "shared_with_firm": bool(r["shared_with_firm"]),
        "pinned": bool(r["pinned"]),
        "user_id": str(r["user_id"]) if r["user_id"] else None,
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


@router.get("")
async def list_reports(
    scope: Optional[str] = Query(default=None),
    principal: Principal = Depends(get_current_firm),
):
    where = ["firm_id = $1::uuid", "(user_id = $2::uuid or shared_with_firm = true)"]
    args: list = [principal.firm_id, principal.user_id]
    idx = 3
    if scope:
        if scope not in VALID_SCOPES:
            raise HTTPException(400, f"scope inválido (válidos: {sorted(VALID_SCOPES)})")
        where.append(f"scope = ${idx}"); args.append(scope); idx += 1
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            select id, name, description, scope, config, shared_with_firm,
                   pinned, user_id, created_at, updated_at
              from report_definitions
             where {' and '.join(where)}
             order by pinned desc, updated_at desc
            """,
            *args,
        )
    return {"items": [_serialize(r) for r in rows]}


@router.get("/{report_id}")
async def get_report(
    report_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select * from report_definitions
             where firm_id = $1::uuid and id = $2::uuid
               and (user_id = $3::uuid or shared_with_firm = true)
            """,
            principal.firm_id, report_id, principal.user_id,
        )
    if not row:
        raise HTTPException(404, "Report no encontrado")
    return _serialize(row)


@router.post("", status_code=201)
async def create_report(
    body: ReportIn,
    principal: Principal = Depends(get_current_firm),
):
    if body.scope not in VALID_SCOPES:
        raise HTTPException(400, f"scope inválido (válidos: {sorted(VALID_SCOPES)})")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                insert into report_definitions
                  (firm_id, user_id, name, description, scope, config,
                   shared_with_firm, pinned)
                values ($1::uuid, $2::uuid, $3, $4, $5, $6::jsonb, $7, $8)
                returning *
                """,
                principal.firm_id, principal.user_id, body.name, body.description,
                body.scope, json.dumps(body.config or {}),
                body.shared_with_firm, body.pinned,
            )
        except Exception as e:
            msg = str(e).lower()
            if "unique" in msg or "duplicate" in msg:
                raise HTTPException(409, "Ya tienes un report con ese nombre")
            raise HTTPException(400, f"No se pudo crear: {e}")
    return _serialize(row)


@router.patch("/{report_id}")
async def update_report(
    report_id: str,
    body: ReportPatch,
    principal: Principal = Depends(get_current_firm),
):
    sets: list[str] = []
    args: list = []
    idx = 1
    if body.name is not None:
        sets.append(f"name = ${idx}"); args.append(body.name); idx += 1
    if body.description is not None:
        sets.append(f"description = ${idx}"); args.append(body.description); idx += 1
    if body.config is not None:
        sets.append(f"config = ${idx}::jsonb"); args.append(json.dumps(body.config)); idx += 1
    if body.shared_with_firm is not None:
        sets.append(f"shared_with_firm = ${idx}"); args.append(body.shared_with_firm); idx += 1
    if body.pinned is not None:
        sets.append(f"pinned = ${idx}"); args.append(body.pinned); idx += 1
    if not sets:
        raise HTTPException(400, "Sin cambios")
    args.append(principal.firm_id)
    args.append(principal.user_id)
    args.append(report_id)
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            update report_definitions set {', '.join(sets)}
             where firm_id = ${idx}::uuid and user_id = ${idx + 1}::uuid and id = ${idx + 2}::uuid
             returning *
            """,
            *args,
        )
    if not row:
        raise HTTPException(404, "Report no encontrado")
    return _serialize(row)


@router.delete("/{report_id}")
async def delete_report(
    report_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "delete from report_definitions where firm_id = $1::uuid and user_id = $2::uuid and id = $3::uuid",
            principal.firm_id, principal.user_id, report_id,
        )
    return {"deleted": True}


# ----------------------------------------------------------------
# Run
# ----------------------------------------------------------------
@router.post("/{report_id}/run")
async def run_report(
    report_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")

    async with storage.pool.acquire() as conn:
        rd = await conn.fetchrow(
            """
            select * from report_definitions
             where firm_id = $1::uuid and id = $2::uuid
               and (user_id = $3::uuid or shared_with_firm = true)
            """,
            principal.firm_id, report_id, principal.user_id,
        )
    if not rd:
        raise HTTPException(404, "Report no encontrado")

    cfg = rd["config"] if not isinstance(rd["config"], str) else (json.loads(rd["config"]) if rd["config"] else {})
    started = time.monotonic()
    result: dict = {}
    status = "ok"
    error: Optional[str] = None
    try:
        result = await _execute_report(principal.firm_id, rd["scope"], cfg)
    except Exception as e:
        status = "failed"
        error = str(e)[:500]
        logger.exception("run_report failed (scope=%s)", rd["scope"])
    duration_ms = int((time.monotonic() - started) * 1000)
    row_count = _row_count_from_result(result)

    params_hash = hashlib.sha1(json.dumps({"scope": rd["scope"], "config": cfg}, sort_keys=True).encode()).hexdigest()[:16]

    async with storage.pool.acquire() as conn:
        run_row = await conn.fetchrow(
            """
            insert into report_runs
              (firm_id, report_id, user_id, params_hash, result, row_count,
               duration_ms, status, error)
            values ($1::uuid, $2::uuid, $3::uuid, $4, $5::jsonb, $6, $7, $8, $9)
            returning id, ran_at
            """,
            principal.firm_id, report_id, principal.user_id, params_hash,
            json.dumps(result), row_count, duration_ms, status, error,
        )
    return {
        "run_id": str(run_row["id"]),
        "ran_at": run_row["ran_at"].isoformat() if run_row["ran_at"] else None,
        "scope": rd["scope"],
        "status": status,
        "duration_ms": duration_ms,
        "row_count": row_count,
        "result": result,
        "error": error,
    }


@router.get("/{report_id}/runs")
async def list_runs(
    report_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, params_hash, row_count, duration_ms, status, error, ran_at
              from report_runs
             where firm_id = $1::uuid and report_id = $2::uuid
             order by ran_at desc
             limit $3
            """,
            principal.firm_id, report_id, limit,
        )
    return {
        "items": [
            {
                "id": str(r["id"]),
                "params_hash": r["params_hash"],
                "row_count": int(r["row_count"] or 0),
                "duration_ms": int(r["duration_ms"] or 0),
                "status": r["status"],
                "error": r["error"],
                "ran_at": r["ran_at"].isoformat() if r["ran_at"] else None,
            }
            for r in rows
        ]
    }


# ----------------------------------------------------------------
# Internal: scope → RPC dispatcher
# ----------------------------------------------------------------
async def _execute_report(firm_id: str, scope: str, cfg: dict) -> dict:
    from utils.db import get_storage
    storage = await get_storage()
    period_days = int(cfg.get("period_days") or 30)
    months = int(cfg.get("months") or 12)

    async with storage.pool.acquire() as conn:
        if scope == "revenue":
            rows = await conn.fetch("select * from lexai_revenue_trend($1::uuid, $2)", firm_id, months)
            return {
                "rows": [
                    {
                        "month": r["month_start"].isoformat() if r["month_start"] else None,
                        "invoiced_cop": float(r["invoiced_cop"] or 0),
                        "collected_cop": float(r["collected_cop"] or 0),
                        "outstanding_cop": float(r["outstanding_cop"] or 0),
                        "invoices_count": int(r["invoices_count"] or 0),
                    }
                    for r in rows
                ]
            }
        if scope == "performance":
            rows = await conn.fetch("select * from lexai_lawyer_performance($1::uuid, $2)", firm_id, period_days)
            return {
                "rows": [
                    {
                        "user_id": str(r["user_id"]),
                        "full_name": r["full_name"],
                        "billable_minutes": int(r["billable_minutes"] or 0),
                        "non_billable_minutes": int(r["non_billable_minutes"] or 0),
                        "matters_count": int(r["matters_count"] or 0),
                        "invoiced_cop": float(r["invoiced_cop"] or 0),
                        "tasks_completed": int(r["tasks_completed"] or 0),
                    }
                    for r in rows
                ]
            }
        if scope == "pipeline":
            rows = await conn.fetch("select * from lexai_pipeline_funnel($1::uuid, $2)", firm_id, period_days)
            return {
                "rows": [
                    {"stage": r["stage"], "count": int(r["count"] or 0), "amount_cop": float(r["amount_cop"] or 0)}
                    for r in rows
                ]
            }
        if scope == "predictions":
            raw = await conn.fetchval("select lexai_prediction_accuracy($1::uuid, $2)", firm_id, period_days)
            return raw if not isinstance(raw, str) else json.loads(raw)
        if scope == "matters":
            rows = await conn.fetch(
                """
                select materia, status, count(*)::int as count
                  from matters where firm_id = $1::uuid
                  group by materia, status
                  order by materia, status
                """, firm_id,
            )
            return {"rows": [{"materia": r["materia"], "status": r["status"], "count": int(r["count"])} for r in rows]}
        if scope == "time":
            rows = await conn.fetch(
                """
                select u.full_name,
                       sum(te.duration_min) filter (where te.billable) as billable,
                       sum(te.duration_min) filter (where not te.billable) as non_billable
                  from time_entries te
                  join users u on u.id = te.user_id
                 where te.firm_id = $1::uuid
                   and te.ended_at is not null
                   and te.ended_at >= now() - make_interval(days => $2)
                 group by u.full_name
                 order by billable desc nulls last
                """,
                firm_id, period_days,
            )
            return {
                "rows": [
                    {
                        "full_name": r["full_name"],
                        "billable_minutes": int(r["billable"] or 0),
                        "non_billable_minutes": int(r["non_billable"] or 0),
                    }
                    for r in rows
                ]
            }
    return {"rows": []}


def _row_count_from_result(result) -> int:
    if isinstance(result, dict) and isinstance(result.get("rows"), list):
        return len(result["rows"])
    if isinstance(result, list):
        return len(result)
    return 0
