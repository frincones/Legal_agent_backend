"""Sprint 18 · Voice tools de analytics ejecutivos.

  · firm_revenue(months?)        → revenue trend resumido
  · lawyer_performance(days?)    → top 5 abogados por billable
  · prediction_accuracy(days?)   → accuracy de las predicciones IA
  · executive_kpis()             → snapshot de hoy en una respuesta

ctx esperado: firm_id, user_id.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


async def firm_revenue_tool(args: dict, ctx: dict) -> dict:
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    months = int(args.get("months", 6))
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch("select * from lexai_revenue_trend($1::uuid, $2)", firm_id, months)
    total_inv = sum(float(r["invoiced_cop"] or 0) for r in rows)
    total_col = sum(float(r["collected_cop"] or 0) for r in rows)
    return {
        "months": months,
        "summary": f"En los últimos {months} meses: facturado ${total_inv:,.0f} COP, cobrado ${total_col:,.0f} COP",
        "invoiced_cop_total": total_inv,
        "collected_cop_total": total_col,
        "realization_pct": round((total_col / total_inv) * 100, 1) if total_inv else 0.0,
    }


async def lawyer_performance_tool(args: dict, ctx: dict) -> dict:
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    days = int(args.get("days", 30))
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch("select * from lexai_lawyer_performance($1::uuid, $2)", firm_id, days)
    top = [
        {
            "full_name": r["full_name"],
            "billable_hours": round(int(r["billable_minutes"] or 0) / 60, 1),
            "matters": int(r["matters_count"] or 0),
            "tasks_completed": int(r["tasks_completed"] or 0),
        }
        for r in rows[:5]
    ]
    return {"days": days, "top_lawyers": top, "count": len(top)}


async def prediction_accuracy_tool(args: dict, ctx: dict) -> dict:
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    days = int(args.get("days", 180))
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {}
    async with storage.pool.acquire() as conn:
        raw = await conn.fetchval("select lexai_prediction_accuracy($1::uuid, $2)", firm_id, days)
    data = raw if isinstance(raw, dict) else (json.loads(raw) if isinstance(raw, str) and raw else {})
    sample = data.get("sample_size", 0) or 0
    if sample == 0:
        return {"summary": "Aún no hay suficientes casos cerrados para medir accuracy", "data": data}
    return {
        "summary": f"En los últimos {days} días, las predicciones acertaron {data.get('accuracy_pct', 0)}% ({data.get('correct', 0)}/{sample})",
        "days": days,
        "data": data,
    }


async def executive_kpis_tool(args: dict, ctx: dict) -> dict:
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {}
    async with storage.pool.acquire() as conn:
        raw = await conn.fetchval("select lexai_executive_kpis($1::uuid)", firm_id)
    data = raw if isinstance(raw, dict) else (json.loads(raw) if isinstance(raw, str) and raw else {})
    return {
        "matters_active": data.get("matters_active", 0),
        "matters_closed_30d": data.get("matters_closed_30d", 0),
        "invoiced_mtd_cop": data.get("invoiced_mtd_cop", 0),
        "collected_mtd_cop": data.get("collected_mtd_cop", 0),
        "ar_overdue_cop": data.get("ar_overdue_cop", 0),
        "tasks_overdue": data.get("tasks_overdue", 0),
        "leads_open": data.get("leads_open", 0),
        "summary": (
            f"{data.get('matters_active', 0)} casos activos · "
            f"facturado este mes ${data.get('invoiced_mtd_cop', 0):,.0f} COP · "
            f"vencido ${data.get('ar_overdue_cop', 0):,.0f} COP"
        ),
    }
