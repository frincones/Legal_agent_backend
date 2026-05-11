"""Sprint 5 · SLA admin endpoint.

POST /v1/sla/run-now    · ejecuta SLA reminders worker para la firma actual
POST /v1/sla/briefing   · genera daily_briefing y lo devuelve (no envía push)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/sla", tags=["sla"])


@router.post("/run-now")
async def run_now(principal: Principal = Depends(get_current_firm)):
    if principal.role not in ("admin", "socio_senior", "socio_junior"):
        raise HTTPException(403, "Solo socios y admins pueden disparar SLA reminders")
    from agent.workers.sla_reminders import run_sla_reminders
    return await run_sla_reminders(principal.firm_id)


@router.post("/briefing")
async def briefing(principal: Principal = Depends(get_current_firm)):
    from agent.tools.daily_briefing import daily_briefing_tool
    return await daily_briefing_tool({}, {"firm_id": principal.firm_id, "user_id": principal.user_id})
