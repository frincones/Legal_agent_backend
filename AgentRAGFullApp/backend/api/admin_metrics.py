"""Sprint 24 · SaaS Admin · Metrics dashboard.

Endpoints:
  GET   /v1/admin/metrics/overview     · KPIs principales (MRR, ARR, ARPU, churn, signups)
  GET   /v1/admin/metrics/timeseries   · series mensuales de signups/MRR (últimos N meses)
  GET   /v1/admin/metrics/plan-distribution · cuántas firms por plan
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from utils.admin_guard import AdminPrincipal, require_saas_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/admin/metrics", tags=["admin-metrics"])


@router.get("/overview")
async def metrics_overview(
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        mrr = await conn.fetchval("select lexai_saas_mrr()")
        signups = await conn.fetchval("select lexai_saas_signups_mtd()")
        churn = await conn.fetchval("select lexai_saas_churn_30d()")
        # Cartera kpi
        cartera = await conn.fetchrow(
            """
            select
              coalesce(sum(case when status in ('sent','overdue','partially_paid')
                            then total_cop - paid_amount_cop else 0 end), 0)::bigint as cartera_total,
              coalesce(sum(case when paid_at >= date_trunc('month', now())
                            then paid_amount_cop else 0 end), 0)::bigint as recaudado_mtd
              from invoices
            """
        )
        # Support tickets KPI
        tickets = await conn.fetchrow(
            """
            select
              count(*) filter (where status = 'open') as open,
              count(*) filter (where status = 'in_progress') as in_progress,
              count(*) filter (where status in ('open','in_progress','waiting_user')) as active
              from support_tickets
            """
        )
    return {
        "mrr": mrr,
        "signups": signups,
        "churn": churn,
        "cartera": dict(cartera) if cartera else {},
        "tickets": dict(tickets) if tickets else {},
    }


@router.get("/timeseries")
async def metrics_timeseries(
    request: Request,
    months: int = 6,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            with months as (
              select generate_series(
                date_trunc('month', now()) - ($1 || ' months')::interval,
                date_trunc('month', now()),
                '1 month'::interval
              )::date as m
            )
            select
              m.m as month_start,
              (select count(*) from firms where date_trunc('month', created_at) = m.m) as signups,
              (select count(*) from firm_subscriptions
                 where status = 'active' and current_period_start <= m.m + interval '1 month'
                   and (current_period_end is null or current_period_end >= m.m)) as active_subs,
              (select coalesce(sum(
                  case
                    when s.billing_period = 'monthly' then p.monthly_cop
                    when s.billing_period = 'annual'  then p.annual_cop / 12
                    else 0
                  end), 0)::bigint
                 from firm_subscriptions s
                 join subscription_plans p on p.code = s.plan_code
                where s.status = 'active'
                  and s.current_period_start <= m.m + interval '1 month'
                  and (s.current_period_end is null or s.current_period_end >= m.m)) as mrr_cop
              from months m
              order by m.m
            """, str(months),
        )
    return {"items": [dict(r) for r in rows]}


@router.get("/audit-log")
async def admin_audit_log(
    request: Request,
    limit: int = 100,
    offset: int = 0,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    """Listado de acciones admin auditadas."""
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, firm_id, user_id, action, resource_type, resource_id,
                   outcome, reason, occurred_at, metadata
              from admin_audit_recent
             order by occurred_at desc
             limit $1 offset $2
            """, limit, offset,
        )
    return {"items": [dict(r) for r in rows]}


@router.get("/plan-distribution")
async def plan_distribution(
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select
              s.plan_code,
              p.name as plan_name,
              count(*) as firms_count,
              count(*) filter (where s.status = 'active') as active,
              count(*) filter (where s.status = 'trialing') as trialing,
              count(*) filter (where s.status = 'past_due') as past_due,
              count(*) filter (where s.status = 'canceled') as canceled
              from firm_subscriptions s
              join subscription_plans p on p.code = s.plan_code
             group by s.plan_code, p.name, p.monthly_cop
             order by p.monthly_cop
            """
        )
    return {"items": [dict(r) for r in rows]}
