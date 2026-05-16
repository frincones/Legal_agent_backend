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

from fastapi import APIRouter, Depends, HTTPException, Request
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
    # Sprint F · acepta los 5 planes (free + starter + pro + firm + enterprise)
    plan_code: str = Field(pattern="^(free|starter|pro|firm|enterprise)$")
    status: str = Field(default="active", pattern="^(active|trialing|past_due|canceled|paused)$")
    billing_period: str = Field(default="monthly", pattern="^(monthly|annual)$")
    extend_trial_days: int = Field(default=0, ge=0, le=365)
    # Sprint F · reason obligatorio para audit
    reason: Optional[str] = Field(default=None, max_length=500)
    # Sprint F · sincronizar con Paddle si existe paddle_subscription_id
    sync_paddle: bool = True


@router.post("/firms/{firm_id}/set-plan")
async def set_firm_plan(
    firm_id: str,
    body: SetPlanRequest,
    request: Request,
    principal: Principal = Depends(get_current_firm),
):
    """Sprint F · cambia el plan de una firma · admin SaaS only.

    Flow:
      1. Resolver admin SaaS (require_saas_admin · soporta legacy _require_admin fallback)
      2. Validar reason (min 5 chars · obligatorio)
      3. UPSERT firm_subscriptions con overrides.manual_plan_override=true
      4. Sincronizar Paddle si existe paddle_subscription_id (billing_mode=full_immediately)
      5. Invalidar cache entitlements
      6. Enviar email notification al admin de la firma
      7. Audit log
      8. Retornar billing_overview + paddle_sync_status + email_sent
    """
    # 1) Auth · prefer require_saas_admin (sprint 24) si el caller lo es,
    # fallback a admin firma (compat con sprint 23)
    is_saas_admin = False
    try:
        from utils.admin_guard import _ensure_bootstrap_admin
        await _ensure_bootstrap_admin(principal)
        is_saas_admin = True
    except Exception:
        pass
    if not is_saas_admin:
        # Verificar admin_users active
        from utils.db import get_storage
        storage = await get_storage()
        async with storage.pool.acquire() as conn:
            row = await conn.fetchrow(
                """select 1 from admin_users
                    where auth_user_id = $1::uuid and active = true""",
                principal.user_id,
            )
            if row:
                is_saas_admin = True
    if not is_saas_admin:
        # Último fallback · admin de la firma (legacy)
        if principal.role != "admin":
            raise HTTPException(403, "Solo admin SaaS o admin de firma puede cambiar plan")

    # 2) Validate reason
    reason = (body.reason or "").strip()
    if len(reason) < 5:
        raise HTTPException(
            400,
            detail={"error": "reason_required",
                    "message": "Provee una razón con mínimo 5 caracteres (audit)."},
        )

    from utils.db import get_storage
    import json
    storage = await get_storage()

    # 3) Read current state (audit + Paddle decision)
    async with storage.pool.acquire() as conn:
        current = await conn.fetchrow(
            """select plan_code, status, billing_period, paddle_subscription_id, overrides
                 from firm_subscriptions where firm_id = $1::uuid""",
            firm_id,
        )
        firm_row = await conn.fetchrow(
            """select razon_social,
                    (select email from users
                      where firm_id = $1::uuid and role = 'admin' and active = true
                      order by created_at asc limit 1) as admin_email
                 from firms where id = $1::uuid""",
            firm_id,
        )

    old_plan_code = current["plan_code"] if current else "free"
    old_overrides = current.get("overrides") if current else {}
    if isinstance(old_overrides, str):
        try:
            old_overrides = json.loads(old_overrides)
        except Exception:
            old_overrides = {}
    paddle_sub_id = current.get("paddle_subscription_id") if current else None
    firm_name = (firm_row.get("razon_social") if firm_row else None) or "(firma)"
    admin_email = firm_row.get("admin_email") if firm_row else None

    # 4) UPSERT firm_subscriptions with override flag
    new_overrides = dict(old_overrides or {})
    new_overrides["manual_plan_override"] = True
    new_overrides["last_change_reason"] = reason
    new_overrides["last_change_by"] = str(principal.user_id)
    from datetime import datetime, timezone
    new_overrides["last_change_at"] = datetime.now(timezone.utc).isoformat()

    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            insert into firm_subscriptions
              (firm_id, plan_code, status, billing_period,
               current_period_start, current_period_end, trial_ends_at, overrides)
            values
              ($1::uuid, $2, $3, $4,
               now(),
               now() + interval '1 month',
               case when $5::int > 0 then now() + ($5::text || ' days')::interval else null end,
               $6::jsonb)
            on conflict (firm_id) do update set
              plan_code = excluded.plan_code,
              status = excluded.status,
              billing_period = excluded.billing_period,
              current_period_start = excluded.current_period_start,
              current_period_end = excluded.current_period_end,
              trial_ends_at = coalesce(excluded.trial_ends_at, firm_subscriptions.trial_ends_at),
              overrides = excluded.overrides,
              updated_at = now()
            """,
            firm_id, body.plan_code, body.status, body.billing_period,
            body.extend_trial_days, json.dumps(new_overrides),
        )

    # 5) Sincronizar Paddle
    paddle_sync_status = "skipped_no_paddle_id"
    if body.sync_paddle and paddle_sub_id:
        try:
            # Lookup new price_id en subscription_plans
            async with storage.pool.acquire() as conn:
                price_row = await conn.fetchrow(
                    "select paddle_price_id from subscription_plans where code = $1",
                    body.plan_code,
                )
            new_price_id = price_row["paddle_price_id"] if price_row else None
            if new_price_id:
                from utils.paddle_client import PaddleClient
                pc = PaddleClient()
                # billing_mode=full_immediately · cobra el costo del nuevo plan
                # y reinicia el ciclo desde hoy
                result = await pc.update_subscription_plan(
                    paddle_sub_id, new_price_id,
                    billing_mode="full_immediately",
                )
                paddle_sync_status = "synced" if result.get("ok") else "error"
            else:
                paddle_sync_status = "skipped_no_price_id"
        except Exception as e:
            logger.warning("Paddle sync failed: %s", e)
            paddle_sync_status = f"error: {str(e)[:60]}"

    # 6) Invalidate entitlements cache
    try:
        from utils.entitlements import invalidate_cache
        invalidate_cache(firm_id)
    except Exception as e:
        logger.debug("entitlements invalidate non-fatal: %s", e)

    # 7) Email notification al admin de la firma
    email_sent = False
    if admin_email:
        try:
            from utils.admin_emails import send_plan_change_notification
            # Get plan display name + monthly_cop
            async with storage.pool.acquire() as conn:
                plan_meta = await conn.fetchrow(
                    "select name, monthly_cop, annual_cop from subscription_plans where code = $1",
                    body.plan_code,
                )
                old_plan_meta = await conn.fetchrow(
                    "select name from subscription_plans where code = $1",
                    old_plan_code,
                )
            email_result = await send_plan_change_notification(
                to_email=admin_email,
                firm_name=firm_name,
                old_plan_name=(old_plan_meta["name"] if old_plan_meta else old_plan_code),
                new_plan_name=(plan_meta["name"] if plan_meta else body.plan_code),
                new_plan_monthly_cop=int(plan_meta["monthly_cop"]) if plan_meta else 0,
                billing_period=body.billing_period,
                reason=reason,
                paddle_synced=(paddle_sync_status == "synced"),
            )
            email_sent = email_result.get("ok", False)
        except Exception as e:
            logger.warning("plan-change email failed (non-fatal): %s", e)

    # 8) Audit log
    try:
        from utils.admin_guard import audit_admin_action
        await audit_admin_action(
            principal=principal,
            scope="saas_admin",
            action="plans.change_membership",
            target_type="firm",
            target_id=str(firm_id),
            details={
                "old_plan": old_plan_code,
                "new_plan": body.plan_code,
                "new_status": body.status,
                "billing_period": body.billing_period,
                "reason": reason,
                "paddle_sync_status": paddle_sync_status,
                "email_sent": email_sent,
                "extend_trial_days": body.extend_trial_days,
            },
            ip=getattr(request.client, "host", None) if request and request.client else None,
        )
    except Exception as e:
        logger.warning("audit log failed (non-fatal): %s", e)

    return {
        "ok": True,
        "firm_id": firm_id,
        "old_plan": old_plan_code,
        "new_plan": body.plan_code,
        "status": body.status,
        "billing_period": body.billing_period,
        "paddle_sync_status": paddle_sync_status,
        "email_sent": email_sent,
        "audit_logged": True,
    }


# Sprint F · catálogo de planes para dropdown UI
@router.get("/subscription-plans")
async def list_subscription_plans(
    principal: Principal = Depends(get_current_firm),
):
    """Lista planes disponibles para el selector del modal admin.

    Sprint F · auth flexible: bootstrap admin > admin_users.active >
    admin de firma. Esto permite que admin SaaS desde /saas/tenants/* lo lea
    sin necesidad de ser admin de la firma específica que está mirando.
    """
    # Auth flexible · prefer SaaS admin · fallback admin firma
    is_authorized = False
    try:
        from utils.admin_guard import _ensure_bootstrap_admin
        await _ensure_bootstrap_admin(principal)
        is_authorized = True
    except Exception:
        pass
    if not is_authorized:
        from utils.db import get_storage as _gs
        storage = await _gs()
        async with storage.pool.acquire() as conn:
            row = await conn.fetchrow(
                """select 1 from admin_users
                    where auth_user_id = $1::uuid and active = true""",
                principal.user_id,
            )
            if row:
                is_authorized = True
    if not is_authorized and principal.role == "admin":
        is_authorized = True
    if not is_authorized:
        raise HTTPException(403, "Solo admin SaaS o admin de firma")

    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """select code, name, monthly_cop, annual_cop,
                      q_users, q_matters, q_documents_mo, q_llm_calls_mo,
                      q_voice_min_mo, paddle_price_id
                 from subscription_plans
                 order by case code when 'free' then 0 when 'starter' then 1
                          when 'pro' then 2 when 'firm' then 3 else 4 end"""
        )
    return [dict(r) for r in rows]


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
