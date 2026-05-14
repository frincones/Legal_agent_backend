"""Sprint 24 · SaaS Admin · Tenants (firms) management.

Endpoints (todos gated por require_saas_admin · audit auto):
  GET    /v1/admin/tenants                · listado paginado + filtros
  GET    /v1/admin/tenants/{firm_id}      · detalle + health
  PATCH  /v1/admin/tenants/{firm_id}      · update razón social / país / metadata
  POST   /v1/admin/tenants/{firm_id}/suspend       · suspend (status=canceled)
  POST   /v1/admin/tenants/{firm_id}/reactivate    · revertir suspend
  GET    /v1/admin/tenants/{firm_id}/users         · usuarios de la firm
  GET    /v1/admin/tenants/{firm_id}/usage         · histórico de usage_events
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from utils.admin_guard import (
    AdminPrincipal, require_saas_admin, require_saas_admin_role, audit_admin_action,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/admin/tenants", tags=["admin-tenants"])


@router.get("")
async def list_tenants(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    plan_code: Optional[str] = None,
    status: Optional[str] = None,
    country: Optional[str] = None,
    q: Optional[str] = None,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    where = ["1=1"]
    params: list = []
    if plan_code:
        params.append(plan_code)
        where.append(f"v.plan_code = ${len(params)}")
    if status:
        params.append(status)
        where.append(f"v.status = ${len(params)}")
    if country:
        params.append(country)
        where.append(f"v.country = ${len(params)}")
    if q:
        params.append(f"%{q.lower()}%")
        where.append(f"lower(v.razon_social) like ${len(params)}")
    params.extend([limit, offset])
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            select v.firm_id, v.razon_social, v.country, v.firm_created_at,
                   v.plan_code, v.plan_name, v.status, v.billing_period,
                   v.current_period_end, v.trial_ends_at, v.canceled_at,
                   v.llm_calls_mtd, v.voice_min_mtd, v.documents_mtd,
                   (select count(*) from users u where u.firm_id = v.firm_id) as users_count,
                   (select count(*) from matters m where m.firm_id = v.firm_id) as matters_count
              from firm_billing_overview v
             where {' and '.join(where)}
             order by v.firm_created_at desc
             limit ${len(params) - 1} offset ${len(params)}
            """,
            *params,
        )
        total = await conn.fetchval("select count(*) from firms")
    await audit_admin_action(admin, "tenants.list", request=request,
                             metadata={"filters": {"plan_code": plan_code, "status": status, "q": q}})
    return {"items": [dict(r) for r in rows], "total": total or 0, "limit": limit, "offset": offset}


@router.get("/{firm_id}")
async def tenant_detail(
    firm_id: str,
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        firm = await conn.fetchrow("select * from firms where id = $1::uuid", firm_id)
        if not firm:
            raise HTTPException(404, "firm not found")
        health = await conn.fetchval("select lexai_saas_firm_health($1::uuid)", firm_id)
        overview = await conn.fetchrow(
            "select * from firm_billing_overview where firm_id = $1::uuid", firm_id,
        )
        flags = await conn.fetch(
            """
            select ffo.flag_key, ffo.enabled, ffo.reason, ffo.expires_at, ffo.created_at,
                   ff.name, ff.category
              from firm_feature_overrides ffo
              left join feature_flags ff on ff.key = ffo.flag_key
             where ffo.firm_id = $1::uuid
             order by ffo.created_at desc
            """, firm_id,
        )
        recent_audit = await conn.fetch(
            """
            select action, resource_type, resource_id, occurred_at, outcome, user_id, metadata
              from audit_logs
             where firm_id = $1::uuid
             order by occurred_at desc limit 20
            """, firm_id,
        )
    await audit_admin_action(admin, "tenants.read", resource_type="firm",
                             resource_id=firm_id, firm_id=firm_id, request=request)
    return {
        "firm": dict(firm),
        "health": health,
        "billing_overview": dict(overview) if overview else None,
        "feature_overrides": [dict(r) for r in flags],
        "recent_audit": [dict(r) for r in recent_audit],
    }


class TenantPatch(BaseModel):
    razon_social: Optional[str] = None
    country: Optional[str] = None
    domain: Optional[str] = None
    region: Optional[str] = None
    zdr_enterprise: Optional[bool] = None
    metadata: Optional[dict] = None


@router.patch("/{firm_id}")
async def tenant_update(
    firm_id: str,
    body: TenantPatch,
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin")),
):
    import json as _json
    sets: list[str] = []
    params: list = [firm_id]
    if body.razon_social is not None:
        params.append(body.razon_social); sets.append(f"razon_social = ${len(params)}")
    if body.country is not None:
        params.append(body.country); sets.append(f"country = ${len(params)}")
    if body.domain is not None:
        params.append(body.domain); sets.append(f"domain = ${len(params)}")
    if body.region is not None:
        params.append(body.region); sets.append(f"region = ${len(params)}")
    if body.zdr_enterprise is not None:
        params.append(body.zdr_enterprise); sets.append(f"zdr_enterprise = ${len(params)}")
    if body.metadata is not None:
        params.append(_json.dumps(body.metadata)); sets.append(f"metadata = ${len(params)}::jsonb")
    if not sets:
        raise HTTPException(400, "Nada que actualizar")
    sets.append("updated_at = now()")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            f"update firms set {', '.join(sets)} where id = $1::uuid",
            *params,
        )
    await audit_admin_action(admin, "tenants.update", resource_type="firm",
                             resource_id=firm_id, firm_id=firm_id, request=request,
                             metadata={"changed_fields": list(body.dict(exclude_none=True).keys())})
    return {"ok": True, "firm_id": firm_id}


class SuspendBody(BaseModel):
    reason: Optional[str] = None


@router.post("/{firm_id}/suspend")
async def tenant_suspend(
    firm_id: str,
    body: SuspendBody,
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin")),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        sub = await conn.fetchrow(
            "select status, plan_code from firm_subscriptions where firm_id = $1::uuid", firm_id,
        )
        if not sub:
            raise HTTPException(404, "subscription not found")
        await conn.execute(
            """
            update firm_subscriptions
               set status = 'paused', canceled_at = coalesce(canceled_at, now()), updated_at = now()
             where firm_id = $1::uuid
            """, firm_id,
        )
    await audit_admin_action(admin, "tenants.suspend", resource_type="firm",
                             resource_id=firm_id, firm_id=firm_id, request=request,
                             reason=body.reason)
    return {"ok": True, "firm_id": firm_id, "previous_status": sub["status"]}


@router.post("/{firm_id}/reactivate")
async def tenant_reactivate(
    firm_id: str,
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin")),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            update firm_subscriptions
               set status = 'active', canceled_at = null, updated_at = now()
             where firm_id = $1::uuid
            """, firm_id,
        )
    await audit_admin_action(admin, "tenants.reactivate", resource_type="firm",
                             resource_id=firm_id, firm_id=firm_id, request=request)
    return {"ok": True, "firm_id": firm_id}


@router.get("/{firm_id}/users")
async def tenant_users(
    firm_id: str,
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, email, full_name, role, cedula_profesional,
                   mfa_enrolled, last_login_at, created_at
              from users
             where firm_id = $1::uuid
             order by created_at desc
            """, firm_id,
        )
    await audit_admin_action(admin, "tenants.users.list", resource_type="firm",
                             resource_id=firm_id, firm_id=firm_id, request=request)
    return {"items": [dict(r) for r in rows]}


@router.get("/{firm_id}/usage")
async def tenant_usage(
    firm_id: str,
    request: Request,
    months: int = 3,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select period_start, kind, count, cost_units
              from usage_counters
             where firm_id = $1::uuid
               and period_start >= (date_trunc('month', now()) - ($2 || ' months')::interval)::date
             order by period_start desc, kind
            """, firm_id, str(months),
        )
    return {"items": [dict(r) for r in rows]}
