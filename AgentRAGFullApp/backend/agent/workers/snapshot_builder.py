"""Sprint 18 · Snapshot builder worker.

Itera todos los firms y persiste el snapshot del día actual (o de la fecha
indicada). UPSERT idempotente · safe re-run.

Punto de entrada:
  POST /v2/analytics-admin/snapshot-now            · admin-only, corre TODOS los firms
  POST /v2/analytics-admin/backfill?days=N         · backfill últimos N días

Para producción, un cron Railway invoca el endpoint diario (UTC).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


async def build_snapshot_for_firm(firm_id: str, snapshot_date: Optional[date] = None) -> dict:
    from utils.db import get_storage
    from utils.analytics_aggregator import upsert_snapshot

    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"firm_id": firm_id, "ok": False, "error": "no_storage"}

    snap_date = snapshot_date or date.today()
    try:
        async with storage.pool.acquire() as conn:
            payload = await upsert_snapshot(conn, firm_id, snap_date)
        return {"firm_id": firm_id, "ok": True, "snapshot_date": snap_date.isoformat(),
                "payload_summary": {
                    "matters_total": payload["matters_total"],
                    "invoiced_mtd": payload["invoiced_cop_mtd"],
                    "tasks_open": payload["tasks_open"],
                }}
    except Exception as e:
        logger.exception("snapshot failed for firm %s", firm_id)
        return {"firm_id": firm_id, "ok": False, "error": str(e)[:300]}


async def build_snapshots_for_all_firms(snapshot_date: Optional[date] = None) -> dict:
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "no_storage"}

    async with storage.pool.acquire() as conn:
        firms = await conn.fetch("select id from firms")

    results = []
    for f in firms:
        res = await build_snapshot_for_firm(str(f["id"]), snapshot_date)
        results.append(res)
    ok = sum(1 for r in results if r["ok"])
    return {
        "snapshot_date": (snapshot_date or date.today()).isoformat(),
        "firms_total": len(results),
        "firms_ok": ok,
        "firms_failed": len(results) - ok,
        "results": results,
    }


async def backfill_days_for_all_firms(days: int) -> dict:
    """Backfill últimos N días para todos los firms."""
    if days < 1:
        return {"error": "days must be >= 1"}
    today = date.today()
    out = []
    for n in range(days):
        d = today - timedelta(days=n)
        r = await build_snapshots_for_all_firms(d)
        out.append({"day": d.isoformat(), "firms_ok": r.get("firms_ok"), "firms_failed": r.get("firms_failed")})
    return {"days_processed": len(out), "summary": out}
