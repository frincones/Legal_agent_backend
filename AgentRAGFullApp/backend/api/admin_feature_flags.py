"""Sprint 24 · SaaS Admin · Feature flags (global + per-firm overrides).

Endpoints:
  GET   /v1/admin/feature-flags                · listado global
  POST  /v1/admin/feature-flags                · crear flag global
  PATCH /v1/admin/feature-flags/{key}          · update flag global (rollout_pct, default)
  GET   /v1/admin/feature-flags/{key}/firms    · firms con override de este flag
  POST  /v1/admin/feature-flags/{key}/firms/{firm_id}    · setear override
  DELETE /v1/admin/feature-flags/{key}/firms/{firm_id}   · borrar override

  GET   /v1/admin/firms/{firm_id}/flags-resolved · todos los flags resueltos para una firm
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
router = APIRouter(prefix="/v1/admin", tags=["admin-feature-flags"])


@router.get("/feature-flags")
async def list_flags(
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select ff.*,
                   (select count(*) from firm_feature_overrides ffo where ffo.flag_key = ff.key) as override_count
              from feature_flags ff
             order by ff.category, ff.key
            """
        )
    return {"items": [dict(r) for r in rows]}


class FlagCreate(BaseModel):
    key: str = Field(pattern="^[a-z][a-z0-9_]+$", max_length=64)
    name: str
    description: Optional[str] = None
    category: str = Field(default="general", pattern="^(general|experimental|beta|gated|kill_switch)$")
    default_value: bool = False
    rollout_pct: int = Field(default=0, ge=0, le=100)


@router.post("/feature-flags")
async def create_flag(
    body: FlagCreate,
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin")),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        try:
            await conn.execute(
                """
                insert into feature_flags (key, name, description, category, default_value, rollout_pct)
                values ($1, $2, $3, $4, $5, $6)
                """,
                body.key, body.name, body.description, body.category,
                body.default_value, body.rollout_pct,
            )
        except Exception as e:
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                raise HTTPException(409, "Ya existe un flag con ese key")
            raise
    await audit_admin_action(admin, "feature_flags.create", resource_type="feature_flag",
                             resource_id=body.key, request=request,
                             metadata=body.dict())
    return {"ok": True, "key": body.key}


class FlagPatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = Field(default=None, pattern="^(general|experimental|beta|gated|kill_switch)$")
    default_value: Optional[bool] = None
    rollout_pct: Optional[int] = Field(default=None, ge=0, le=100)


@router.patch("/feature-flags/{key}")
async def update_flag(
    key: str,
    body: FlagPatch,
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin")),
):
    sets: list[str] = []
    params: list = [key]
    for field, value in body.dict(exclude_none=True).items():
        params.append(value); sets.append(f"{field} = ${len(params)}")
    if not sets:
        raise HTTPException(400, "Nada que actualizar")
    sets.append("updated_at = now()")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            f"update feature_flags set {', '.join(sets)} where key = $1",
            *params,
        )
    await audit_admin_action(admin, "feature_flags.update", resource_type="feature_flag",
                             resource_id=key, request=request,
                             metadata={"changes": body.dict(exclude_none=True)})
    return {"ok": True, "key": key}


@router.get("/feature-flags/{key}/firms")
async def list_overrides(
    key: str,
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select ffo.*, f.razon_social
              from firm_feature_overrides ffo
              join firms f on f.id = ffo.firm_id
             where ffo.flag_key = $1
             order by ffo.created_at desc
            """, key,
        )
    return {"items": [dict(r) for r in rows]}


class OverrideBody(BaseModel):
    enabled: bool
    reason: Optional[str] = None
    expires_at: Optional[str] = None  # ISO format


@router.post("/feature-flags/{key}/firms/{firm_id}")
async def set_override(
    key: str,
    firm_id: str,
    body: OverrideBody,
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin")),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            insert into firm_feature_overrides (firm_id, flag_key, enabled, reason, expires_at, created_by)
            values ($1::uuid, $2, $3, $4, $5::timestamptz, $6::uuid)
            on conflict (firm_id, flag_key) do update set
              enabled = excluded.enabled,
              reason = excluded.reason,
              expires_at = excluded.expires_at,
              created_by = excluded.created_by,
              created_at = now()
            """,
            firm_id, key, body.enabled, body.reason, body.expires_at, admin.admin_user_id,
        )
    await audit_admin_action(admin, "feature_flags.override.set", resource_type="feature_flag",
                             resource_id=key, firm_id=firm_id, request=request,
                             metadata={"enabled": body.enabled, "reason": body.reason})
    return {"ok": True}


@router.delete("/feature-flags/{key}/firms/{firm_id}")
async def remove_override(
    key: str,
    firm_id: str,
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin")),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "delete from firm_feature_overrides where flag_key = $1 and firm_id = $2::uuid",
            key, firm_id,
        )
    await audit_admin_action(admin, "feature_flags.override.remove", resource_type="feature_flag",
                             resource_id=key, firm_id=firm_id, request=request)
    return {"ok": True}


@router.get("/firms/{firm_id}/flags-resolved")
async def firm_flags_resolved(
    firm_id: str,
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    """Resuelve todos los feature flags para una firm (override > rollout > default)."""
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select ff.key, ff.name, ff.category, ff.default_value, ff.rollout_pct,
                   lexai_feature_enabled($1::uuid, ff.key) as resolved,
                   ffo.enabled as override_value, ffo.reason as override_reason
              from feature_flags ff
              left join firm_feature_overrides ffo on ffo.flag_key = ff.key and ffo.firm_id = $1::uuid
             order by ff.category, ff.key
            """, firm_id,
        )
    return {"items": [dict(r) for r in rows]}
