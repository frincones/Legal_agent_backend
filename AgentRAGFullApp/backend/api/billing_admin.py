"""Sprint 23 · Admin billing endpoints (panel SaaS admin · M37 preview).

Endpoints (todos requieren role='admin' global · MVP):
  GET  /v1/billing/admin/firms          · listado de firms con overview de plan + uso
  GET  /v1/billing/admin/firms/{id}     · detalle de una firm
  POST /v1/billing/admin/firms/{id}/set-plan        · cambiar plan manual
  POST /v1/billing/admin/firms/{id}/extend-trial    · extender trial N días
  POST /v1/billing/admin/run-worker     · corre billing_worker (expire_trials + rollover)

Diseño:
  - Solo admin GLOBAL (no admin de firma). Para B2B Saas owner-only.
  - Endpoints aditivos al api/billing.py existente.
  - No toca firm_subscriptions vía Paddle (eso es trabajo del webhook).
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/billing/admin", tags=["billing-admin"])


def _require_admin(principal: Principal) -> None:
    """Por ahora 'admin' role de la firma (no global SaaS owner).
    En Sprint 24 reemplazaremos con scope SaaS-owner real."""
    if principal.role != "admin":
        raise HTTPException(403, "Solo admin")


@router.get("/firms")
async def list_firms_billing(
    limit: int = 50,
    offset: int = 0,
    status: Optional[str] = None,
    plan_code: Optional[str] = None,
    principal: Principal = Depends(get_current_firm),
):
    _require_admin(principal)
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        where = ["1=1"]
        params: list = []
        if status:
            params.append(status)
            where.append(f"status = ${len(params)}")
        if plan_code:
            params.append(plan_code)
            where.append(f"plan_code = ${len(params)}")
        params.extend([limit, offset])
        rows = await conn.fetch(
            f"""
            select firm_id, razon_social, country, firm_created_at,
                   plan_code, plan_name, status, billing_period,
                   current_period_start, current_period_end, trial_ends_at,
                   canceled_at, paddle_subscription_id,
                   monthly_cop, annual_cop,
                   llm_calls_mtd, voice_min_mtd, documents_mtd
              from firm_billing_overview
             where {' and '.join(where)}
             order by firm_created_at desc
             limit ${len(params) - 1} offset ${len(params)}
            """,
            *params,
        )
        total = await conn.fetchval("select count(*) from firm_billing_overview")
    return {
        "items": [dict(r) for r in rows],
        "total": total or 0,
        "limit": limit,
        "offset": offset,
    }


@router.get("/firms/{firm_id}")
async def firm_billing_detail(firm_id: str, principal: Principal = Depends(get_current_firm)):
    _require_admin(principal)
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        overview = await conn.fetchrow(
            "select * from firm_billing_overview where firm_id = $1::uuid",
            firm_id,
        )
        if not overview:
            raise HTTPException(404, "firm not found")
        events_count = await conn.fetchval(
            "select count(*) from usage_events where firm_id = $1::uuid and occurred_at >= date_trunc('month', now())",
            firm_id,
        )
        webhooks = await conn.fetch(
            """
            select event_id, event_type, processed, received_at, error
              from paddle_webhook_events
             where firm_id = $1::uuid
             order by received_at desc limit 20
            """,
            firm_id,
        )
        quota = await conn.fetchval("select lexai_quota_status($1::uuid)", firm_id)
    return {
        "overview": dict(overview),
        "events_mtd": events_count or 0,
        "recent_webhooks": [dict(w) for w in webhooks],
        "quota_status": quota,
    }


class SetPlanRequest(BaseModel):
    plan_code: str = Field(pattern="^(free|pro|firm|enterprise)$")
    status: str = Field(default="active", pattern="^(active|trialing|past_due|canceled|paused)$")
    billing_period: str = Field(default="monthly", pattern="^(monthly|annual)$")
    extend_trial_days: int = Field(default=0, ge=0, le=365)


@router.post("/firms/{firm_id}/set-plan")
async def set_firm_plan(
    firm_id: str,
    body: SetPlanRequest,
    principal: Principal = Depends(get_current_firm),
):
    _require_admin(principal)
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            insert into firm_subscriptions
              (firm_id, plan_code, status, billing_period,
               current_period_start, current_period_end, trial_ends_at)
            values
              ($1::uuid, $2, $3, $4,
               date_trunc('month', now()),
               date_trunc('month', now()) + interval '1 month',
               case when $5::int > 0 then now() + ($5::text || ' days')::interval else null end)
            on conflict (firm_id) do update set
              plan_code = excluded.plan_code,
              status = excluded.status,
              billing_period = excluded.billing_period,
              current_period_end = excluded.current_period_end,
              trial_ends_at = coalesce(excluded.trial_ends_at, firm_subscriptions.trial_ends_at),
              updated_at = now()
            """,
            firm_id, body.plan_code, body.status, body.billing_period, body.extend_trial_days,
        )
    return {"ok": True, "firm_id": firm_id, "plan": body.plan_code}


class ExtendTrialRequest(BaseModel):
    days: int = Field(ge=1, le=180)


@router.post("/firms/{firm_id}/extend-trial")
async def extend_trial(
    firm_id: str,
    body: ExtendTrialRequest,
    principal: Principal = Depends(get_current_firm),
):
    _require_admin(principal)
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            "select trial_ends_at, status from firm_subscriptions where firm_id = $1::uuid",
            firm_id,
        )
        if not row:
            raise HTTPException(404, "subscription not found")
        await conn.execute(
            """
            update firm_subscriptions
               set trial_ends_at = coalesce(trial_ends_at, now()) + ($2::text || ' days')::interval,
                   status = case when status = 'past_due' then 'trialing' else status end,
                   updated_at = now()
             where firm_id = $1::uuid
            """,
            firm_id, body.days,
        )
    return {"ok": True, "firm_id": firm_id, "extended_days": body.days}


@router.post("/run-worker")
async def run_billing_worker(principal: Principal = Depends(get_current_firm)):
    _require_admin(principal)
    from agent.workers.billing_worker import run_all
    return await run_all()
