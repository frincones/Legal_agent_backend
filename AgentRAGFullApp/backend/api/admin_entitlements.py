"""Sprint 25 · SaaS Admin · Entitlements management.

Endpoints (todos gated por require_saas_admin · audit auto):

  Modules catalog:
    GET   /v1/admin/modules                · listar todos los módulos
    POST  /v1/admin/modules                · crear módulo nuevo
    PATCH /v1/admin/modules/{key}          · update metadata
    DELETE /v1/admin/modules/{key}         · borrar (cascade plan_modules + overrides)

  Quota types catalog:
    GET   /v1/admin/quota-types
    POST  /v1/admin/quota-types
    PATCH /v1/admin/quota-types/{key}

  Plan bundle editor:
    GET   /v1/admin/plans/{code}/bundle             · módulos + cuotas del plan
    PUT   /v1/admin/plans/{code}/modules            · upsert múltiples plan_modules
    PUT   /v1/admin/plans/{code}/quotas             · upsert múltiples plan_quotas

  Matrix view:
    GET   /v1/admin/matrix/modules         · matriz modules × plans (todo en 1 query)
    GET   /v1/admin/matrix/quotas          · matriz quotas × plans

  Firm overrides:
    GET   /v1/admin/firms/{firm_id}/entitlements              · resuelto completo
    POST  /v1/admin/firms/{firm_id}/module-overrides         · set/unset módulo
    DELETE /v1/admin/firms/{firm_id}/module-overrides/{key}
    POST  /v1/admin/firms/{firm_id}/quota-overrides
    DELETE /v1/admin/firms/{firm_id}/quota-overrides/{key}
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from utils.admin_guard import (
    AdminPrincipal, require_saas_admin, require_saas_admin_role, audit_admin_action,
)
from utils.entitlements import invalidate_cache

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/admin", tags=["admin-entitlements"])


# ══════════════════════════════════════════════════════════════════════
# MODULES catalog
# ══════════════════════════════════════════════════════════════════════


@router.get("/modules")
async def list_modules(admin: AdminPrincipal = Depends(require_saas_admin)):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select m.*,
              (select count(*) from plan_modules pm where pm.module_key = m.key and pm.enabled) as plans_count,
              (select count(*) from firm_module_overrides fmo where fmo.module_key = m.key
                 and (fmo.expires_at is null or fmo.expires_at > now())) as overrides_count
            from modules m
            order by m.category, m.sort_order, m.key
            """,
        )
    return {"items": [dict(r) for r in rows]}


class ModuleCreate(BaseModel):
    key: str = Field(pattern="^[a-z][a-z0-9_]+$", max_length=64)
    name: str
    description: Optional[str] = None
    category: str = Field(pattern="^(core|productivity|ai|docs|calc|collaboration|automation|analytics|integrations|client_facing|billing|marketplace|admin_only|experimental)$")
    ui_route: Optional[str] = None
    is_core: bool = False
    kill_switch_default: bool = False
    sort_order: int = 100


@router.post("/modules")
async def create_module(
    body: ModuleCreate, request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin")),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        try:
            await conn.execute(
                """
                insert into modules (key, name, description, category, ui_route, is_core, kill_switch_default, sort_order)
                values ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                body.key, body.name, body.description, body.category, body.ui_route,
                body.is_core, body.kill_switch_default, body.sort_order,
            )
        except Exception as e:
            if "unique" in str(e).lower():
                raise HTTPException(409, "Ya existe módulo con ese key")
            raise
    await audit_admin_action(admin, "modules.create", resource_type="module",
                             resource_id=body.key, request=request, metadata=body.dict())
    invalidate_cache()
    return {"ok": True, "key": body.key}


class ModulePatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    ui_route: Optional[str] = None
    is_core: Optional[bool] = None
    kill_switch_default: Optional[bool] = None
    sort_order: Optional[int] = None


@router.patch("/modules/{key}")
async def update_module(
    key: str, body: ModulePatch, request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin")),
):
    sets: list[str] = []
    params: list = [key]
    for f, v in body.dict(exclude_none=True).items():
        params.append(v); sets.append(f"{f} = ${len(params)}")
    if not sets:
        raise HTTPException(400, "Nada que actualizar")
    sets.append("updated_at = now()")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(f"update modules set {', '.join(sets)} where key = $1", *params)
    await audit_admin_action(admin, "modules.update", resource_type="module",
                             resource_id=key, request=request, metadata={"changes": body.dict(exclude_none=True)})
    invalidate_cache()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════
# QUOTA TYPES
# ══════════════════════════════════════════════════════════════════════


@router.get("/quota-types")
async def list_quota_types(admin: AdminPrincipal = Depends(require_saas_admin)):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch("select * from quota_types order by sort_order, key")
    return {"items": [dict(r) for r in rows]}


# ══════════════════════════════════════════════════════════════════════
# PLAN BUNDLE EDITOR
# ══════════════════════════════════════════════════════════════════════


@router.get("/plans/{code}/bundle")
async def plan_bundle(code: str, admin: AdminPrincipal = Depends(require_saas_admin)):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        plan = await conn.fetchrow("select * from subscription_plans where code = $1", code)
        if not plan:
            raise HTTPException(404, "plan not found")
        modules = await conn.fetch(
            """
            select m.key, m.name, m.category, m.is_core, m.sort_order,
                   coalesce(pm.enabled, false) as enabled
              from modules m
              left join plan_modules pm on pm.module_key = m.key and pm.plan_code = $1
             order by m.category, m.sort_order
            """, code,
        )
        quotas = await conn.fetch(
            """
            select q.key, q.name, q.unit, q.reset_period, q.enforcement, q.sort_order,
                   pq.limit_value, pq.soft_cap_pct
              from quota_types q
              left join plan_quotas pq on pq.quota_type_key = q.key and pq.plan_code = $1
             order by q.sort_order
            """, code,
        )
    return {
        "plan": dict(plan),
        "modules": [dict(m) for m in modules],
        "quotas": [dict(q) for q in quotas],
    }


class PlanModulesBulk(BaseModel):
    items: list[dict]  # [{module_key, enabled}, ...]


@router.put("/plans/{code}/modules")
async def update_plan_modules(
    code: str, body: PlanModulesBulk, request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin")),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        async with conn.transaction():
            for item in body.items:
                await conn.execute(
                    """
                    insert into plan_modules (plan_code, module_key, enabled)
                    values ($1, $2, $3)
                    on conflict (plan_code, module_key) do update set
                      enabled = excluded.enabled,
                      updated_at = now()
                    """,
                    code, item["module_key"], bool(item["enabled"]),
                )
    await audit_admin_action(admin, "plans.modules.update", resource_type="plan",
                             resource_id=code, request=request,
                             metadata={"count": len(body.items)})
    invalidate_cache()
    return {"ok": True, "updated": len(body.items)}


class PlanQuotasBulk(BaseModel):
    items: list[dict]  # [{quota_type_key, limit_value, soft_cap_pct}, ...]


@router.put("/plans/{code}/quotas")
async def update_plan_quotas(
    code: str, body: PlanQuotasBulk, request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin")),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        async with conn.transaction():
            for item in body.items:
                await conn.execute(
                    """
                    insert into plan_quotas (plan_code, quota_type_key, limit_value, soft_cap_pct)
                    values ($1, $2, $3, $4)
                    on conflict (plan_code, quota_type_key) do update set
                      limit_value = excluded.limit_value,
                      soft_cap_pct = excluded.soft_cap_pct,
                      updated_at = now()
                    """,
                    code, item["quota_type_key"], item.get("limit_value"), item.get("soft_cap_pct", 80),
                )
    await audit_admin_action(admin, "plans.quotas.update", resource_type="plan",
                             resource_id=code, request=request,
                             metadata={"count": len(body.items)})
    invalidate_cache()
    return {"ok": True, "updated": len(body.items)}


# ══════════════════════════════════════════════════════════════════════
# MATRIX VIEWS (módulos × planes, cuotas × planes)
# ══════════════════════════════════════════════════════════════════════


@router.get("/matrix/modules")
async def modules_matrix(admin: AdminPrincipal = Depends(require_saas_admin)):
    """Matriz módulos × planes. Devuelve módulos como rows + planes como cols."""
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        modules = await conn.fetch(
            "select key, name, category, is_core, sort_order from modules order by category, sort_order",
        )
        plans = await conn.fetch(
            "select code, name, monthly_cop from subscription_plans order by monthly_cop",
        )
        cells = await conn.fetch(
            "select plan_code, module_key, enabled from plan_modules",
        )
    matrix = {}
    for c in cells:
        matrix[(c["plan_code"], c["module_key"])] = c["enabled"]
    return {
        "plans": [dict(p) for p in plans],
        "modules": [
            {
                **dict(m),
                "by_plan": {p["code"]: matrix.get((p["code"], m["key"]), False) for p in plans},
            }
            for m in modules
        ],
    }


@router.get("/matrix/quotas")
async def quotas_matrix(admin: AdminPrincipal = Depends(require_saas_admin)):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        quotas = await conn.fetch("select * from quota_types order by sort_order")
        plans = await conn.fetch("select code, name, monthly_cop from subscription_plans order by monthly_cop")
        cells = await conn.fetch("select plan_code, quota_type_key, limit_value, soft_cap_pct from plan_quotas")
    cell_map = {}
    for c in cells:
        cell_map[(c["plan_code"], c["quota_type_key"])] = {"limit_value": c["limit_value"], "soft_cap_pct": c["soft_cap_pct"]}
    return {
        "plans": [dict(p) for p in plans],
        "quotas": [
            {
                **dict(q),
                "by_plan": {p["code"]: cell_map.get((p["code"], q["key"]), {"limit_value": 0, "soft_cap_pct": 80}) for p in plans},
            }
            for q in quotas
        ],
    }


# ══════════════════════════════════════════════════════════════════════
# FIRM OVERRIDES (per-tenant custom entitlements)
# ══════════════════════════════════════════════════════════════════════


@router.get("/firms/{firm_id}/entitlements")
async def firm_entitlements_admin(
    firm_id: str, admin: AdminPrincipal = Depends(require_saas_admin),
):
    """Estado resuelto + overrides activos."""
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        ent = await conn.fetchval("select lexai_entitlements($1::uuid)", firm_id)
        module_overrides = await conn.fetch(
            """
            select fmo.*, m.name as module_name, m.category, au.email as created_by_email
              from firm_module_overrides fmo
              join modules m on m.key = fmo.module_key
              left join admin_users au on au.id = fmo.created_by
             where fmo.firm_id = $1::uuid
             order by fmo.created_at desc
            """, firm_id,
        )
        quota_overrides = await conn.fetch(
            """
            select fqo.*, q.name as quota_name, q.unit, au.email as created_by_email
              from firm_quota_overrides fqo
              join quota_types q on q.key = fqo.quota_type_key
              left join admin_users au on au.id = fqo.created_by
             where fqo.firm_id = $1::uuid
             order by fqo.created_at desc
            """, firm_id,
        )
    return {
        "entitlements": ent,
        "module_overrides": [dict(r) for r in module_overrides],
        "quota_overrides": [dict(r) for r in quota_overrides],
    }


class ModuleOverrideBody(BaseModel):
    module_key: str
    enabled: bool
    reason: str = Field(min_length=5, max_length=500)
    expires_at: Optional[str] = None  # ISO format


@router.post("/firms/{firm_id}/module-overrides")
async def set_module_override(
    firm_id: str, body: ModuleOverrideBody, request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin")),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            insert into firm_module_overrides (firm_id, module_key, enabled, reason, expires_at, created_by)
            values ($1::uuid, $2, $3, $4, $5::timestamptz, $6::uuid)
            on conflict (firm_id, module_key) do update set
              enabled = excluded.enabled,
              reason = excluded.reason,
              expires_at = excluded.expires_at,
              created_by = excluded.created_by,
              created_at = now()
            """,
            firm_id, body.module_key, body.enabled, body.reason, body.expires_at, admin.admin_user_id,
        )
    await audit_admin_action(admin, "firm.module_override.set", resource_type="firm",
                             resource_id=firm_id, firm_id=firm_id, request=request,
                             metadata=body.dict())
    invalidate_cache(firm_id)
    return {"ok": True}


@router.delete("/firms/{firm_id}/module-overrides/{module_key}")
async def remove_module_override(
    firm_id: str, module_key: str, request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin")),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "delete from firm_module_overrides where firm_id = $1::uuid and module_key = $2",
            firm_id, module_key,
        )
    await audit_admin_action(admin, "firm.module_override.remove", resource_type="firm",
                             resource_id=firm_id, firm_id=firm_id, request=request,
                             metadata={"module_key": module_key})
    invalidate_cache(firm_id)
    return {"ok": True}


class QuotaOverrideBody(BaseModel):
    quota_type_key: str
    limit_value: Optional[int] = None  # null = unlimited
    reason: str = Field(min_length=5, max_length=500)
    expires_at: Optional[str] = None


@router.post("/firms/{firm_id}/quota-overrides")
async def set_quota_override(
    firm_id: str, body: QuotaOverrideBody, request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin")),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            insert into firm_quota_overrides (firm_id, quota_type_key, limit_value, reason, expires_at, created_by)
            values ($1::uuid, $2, $3, $4, $5::timestamptz, $6::uuid)
            on conflict (firm_id, quota_type_key) do update set
              limit_value = excluded.limit_value,
              reason = excluded.reason,
              expires_at = excluded.expires_at,
              created_by = excluded.created_by,
              created_at = now()
            """,
            firm_id, body.quota_type_key, body.limit_value, body.reason, body.expires_at, admin.admin_user_id,
        )
    await audit_admin_action(admin, "firm.quota_override.set", resource_type="firm",
                             resource_id=firm_id, firm_id=firm_id, request=request,
                             metadata=body.dict())
    invalidate_cache(firm_id)
    return {"ok": True}


@router.delete("/firms/{firm_id}/quota-overrides/{quota_type_key}")
async def remove_quota_override(
    firm_id: str, quota_type_key: str, request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin")),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "delete from firm_quota_overrides where firm_id = $1::uuid and quota_type_key = $2",
            firm_id, quota_type_key,
        )
    await audit_admin_action(admin, "firm.quota_override.remove", resource_type="firm",
                             resource_id=firm_id, firm_id=firm_id, request=request,
                             metadata={"quota_type_key": quota_type_key})
    invalidate_cache(firm_id)
    return {"ok": True}
