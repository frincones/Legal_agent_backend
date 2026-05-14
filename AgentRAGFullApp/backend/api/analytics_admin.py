"""Sprint 18 · Analytics admin endpoints.

  POST /v2/analytics-admin/snapshot-now       · admin · corre snapshot del firm hoy
  POST /v2/analytics-admin/snapshot-all       · admin · TODOS los firms (cron-style)
  POST /v2/analytics-admin/backfill?days=N    · admin · backfill últimos N días

Para llamadas internas / desde cron: protegido por role.
"""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v2/analytics-admin", tags=["analytics_admin"])


def _require_admin(principal: Principal):
    if principal.role not in ("admin", "socio_senior", "socio_junior"):
        raise HTTPException(403, "Sólo admin / socios")


@router.post("/snapshot-now")
async def snapshot_now(principal: Principal = Depends(get_current_firm)):
    _require_admin(principal)
    from agent.workers.snapshot_builder import build_snapshot_for_firm
    return await build_snapshot_for_firm(principal.firm_id, date.today())


@router.post("/snapshot-all")
async def snapshot_all(principal: Principal = Depends(get_current_firm)):
    _require_admin(principal)
    if principal.role != "admin":
        raise HTTPException(403, "Solo admin global")
    from agent.workers.snapshot_builder import build_snapshots_for_all_firms
    return await build_snapshots_for_all_firms()


@router.post("/backfill")
async def backfill(
    days: int = Query(default=7, ge=1, le=60),
    principal: Principal = Depends(get_current_firm),
):
    _require_admin(principal)
    if principal.role != "admin":
        raise HTTPException(403, "Solo admin global")
    from agent.workers.snapshot_builder import backfill_days_for_all_firms
    return await backfill_days_for_all_firms(days)
