"""Sprint 18 · Analytics v2 API.

Endpoints (todos firm-scoped via Principal):
  GET /v2/analytics/executive-kpis      · KPIs ejecutivos (today)
  GET /v2/analytics/revenue-trend       · series mensuales (12m default)
  GET /v2/analytics/ar-aging            · AR aging buckets
  GET /v2/analytics/performance         · performance por abogado (30d default)
  GET /v2/analytics/pipeline            · funnel leads → matters (90d default)
  GET /v2/analytics/prediction-accuracy · accuracy de predicciones IA
  GET /v2/analytics/snapshots           · serie histórica de snapshots
  GET /v2/analytics/snapshots/latest    · snapshot más reciente

Coexiste con /api/analytics/* (Sprint 7) · no lo reemplaza.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v2/analytics", tags=["analytics_v2"])


def _parse_jsonb(v):
    if v is None:
        return {}
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return {}
    return v


@router.get("/executive-kpis")
async def executive_kpis(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {}
    async with storage.pool.acquire() as conn:
        result = await conn.fetchval(
            "select lexai_executive_kpis($1::uuid)", principal.firm_id,
        )
    return _parse_jsonb(result)


@router.get("/revenue-trend")
async def revenue_trend(
    months: int = Query(default=12, ge=3, le=36),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            "select * from lexai_revenue_trend($1::uuid, $2)",
            principal.firm_id, months,
        )
    return {
        "items": [
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


@router.get("/ar-aging")
async def ar_aging(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {}
    async with storage.pool.acquire() as conn:
        result = await conn.fetchval(
            "select lexai_ar_aging($1::uuid)", principal.firm_id,
        )
    return _parse_jsonb(result)


@router.get("/performance")
async def lawyer_performance(
    days: int = Query(default=30, ge=7, le=365),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            "select * from lexai_lawyer_performance($1::uuid, $2)",
            principal.firm_id, days,
        )
    return {
        "days": days,
        "items": [
            {
                "user_id": str(r["user_id"]),
                "full_name": r["full_name"],
                "avatar_url": r["avatar_url"],
                "billable_minutes": int(r["billable_minutes"] or 0),
                "non_billable_minutes": int(r["non_billable_minutes"] or 0),
                "billable_hours": round((int(r["billable_minutes"] or 0)) / 60, 1),
                "utilization_pct": _utilization(int(r["billable_minutes"] or 0), int(r["non_billable_minutes"] or 0)),
                "matters_count": int(r["matters_count"] or 0),
                "invoiced_cop": float(r["invoiced_cop"] or 0),
                "tasks_completed": int(r["tasks_completed"] or 0),
            }
            for r in rows
        ],
    }


def _utilization(billable: int, non_billable: int) -> float:
    total = billable + non_billable
    if total == 0:
        return 0.0
    return round((billable / total) * 100, 1)


@router.get("/pipeline")
async def pipeline(
    days: int = Query(default=90, ge=7, le=365),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"stages": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            "select * from lexai_pipeline_funnel($1::uuid, $2)",
            principal.firm_id, days,
        )
    stages = [
        {
            "stage": r["stage"],
            "count": int(r["count"] or 0),
            "amount_cop": float(r["amount_cop"] or 0),
        }
        for r in rows
    ]
    # conversion · matters_won / leads_open en window
    leads_open = next((s["count"] for s in stages if s["stage"] == "leads_open"), 0)
    matters_closed = next((s["count"] for s in stages if s["stage"] == "matters_closed"), 0)
    conv = round((matters_closed / leads_open) * 100, 1) if leads_open else 0.0
    return {"days": days, "stages": stages, "conversion_rate_pct": conv}


@router.get("/prediction-accuracy")
async def prediction_accuracy(
    days: int = Query(default=180, ge=30, le=730),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {}
    async with storage.pool.acquire() as conn:
        result = await conn.fetchval(
            "select lexai_prediction_accuracy($1::uuid, $2)",
            principal.firm_id, days,
        )
    return _parse_jsonb(result)


@router.get("/snapshots")
async def snapshots(
    limit: int = Query(default=30, ge=1, le=365),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select snapshot_date, matters_total, matters_active, matters_closed,
                   matters_won, matters_lost,
                   billable_minutes_total, billable_minutes_30d, non_billable_minutes_30d,
                   invoiced_cop_mtd, collected_cop_mtd, ar_total_cop, ar_overdue_cop,
                   leads_open, leads_won_30d, leads_lost_30d,
                   predictions_30d, predictions_reviewed_30d,
                   tasks_open, tasks_overdue, comments_30d,
                   kb_entries_total, lessons_total, payload, computed_at
              from firm_analytics_snapshots
             where firm_id = $1::uuid
             order by snapshot_date desc
             limit $2
            """,
            principal.firm_id, limit,
        )
    return {
        "items": [
            {
                "snapshot_date": r["snapshot_date"].isoformat() if r["snapshot_date"] else None,
                "matters_total": int(r["matters_total"] or 0),
                "matters_active": int(r["matters_active"] or 0),
                "matters_closed": int(r["matters_closed"] or 0),
                "matters_won": int(r["matters_won"] or 0),
                "matters_lost": int(r["matters_lost"] or 0),
                "billable_minutes_total": int(r["billable_minutes_total"] or 0),
                "billable_minutes_30d": int(r["billable_minutes_30d"] or 0),
                "non_billable_minutes_30d": int(r["non_billable_minutes_30d"] or 0),
                "invoiced_cop_mtd": float(r["invoiced_cop_mtd"] or 0),
                "collected_cop_mtd": float(r["collected_cop_mtd"] or 0),
                "ar_total_cop": float(r["ar_total_cop"] or 0),
                "ar_overdue_cop": float(r["ar_overdue_cop"] or 0),
                "leads_open": int(r["leads_open"] or 0),
                "leads_won_30d": int(r["leads_won_30d"] or 0),
                "leads_lost_30d": int(r["leads_lost_30d"] or 0),
                "predictions_30d": int(r["predictions_30d"] or 0),
                "predictions_reviewed_30d": int(r["predictions_reviewed_30d"] or 0),
                "tasks_open": int(r["tasks_open"] or 0),
                "tasks_overdue": int(r["tasks_overdue"] or 0),
                "comments_30d": int(r["comments_30d"] or 0),
                "kb_entries_total": int(r["kb_entries_total"] or 0),
                "lessons_total": int(r["lessons_total"] or 0),
                "payload": _parse_jsonb(r["payload"]),
                "computed_at": r["computed_at"].isoformat() if r["computed_at"] else None,
            }
            for r in rows
        ]
    }


@router.get("/snapshots/latest")
async def snapshot_latest(principal: Principal = Depends(get_current_firm)):
    result = await snapshots(limit=1, principal=principal)  # type: ignore[arg-type]
    items = result.get("items") if isinstance(result, dict) else None
    return items[0] if items else None
