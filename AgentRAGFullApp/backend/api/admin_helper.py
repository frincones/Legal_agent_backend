"""Sprint 26 · Admin · Helper tips CRUD.

Endpoints:
  GET    /v1/admin/helper-tips
  POST   /v1/admin/helper-tips
  PATCH  /v1/admin/helper-tips/{key}
  DELETE /v1/admin/helper-tips/{key}
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
router = APIRouter(prefix="/v1/admin/helper-tips", tags=["admin-helper"])


@router.get("")
async def list_tips(admin: AdminPrincipal = Depends(require_saas_admin)):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            "select * from helper_tips order by category, priority asc, key",
        )
    return {"items": [dict(r) for r in rows]}


class TipCreate(BaseModel):
    key: str = Field(pattern="^[a-z][a-z0-9_]+$", max_length=64)
    route_pattern: Optional[str] = None
    module_key: Optional[str] = None
    title: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=10, max_length=2000)
    cta_label: Optional[str] = None
    cta_href: Optional[str] = None
    priority: int = Field(default=100, ge=0, le=1000)
    active: bool = True
    show_to_roles: Optional[list[str]] = None
    category: str = Field(default="tip", pattern="^(tip|feature|warning|onboarding|keyboard_shortcut)$")


@router.post("")
async def create_tip(
    body: TipCreate, request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin")),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        try:
            await conn.execute(
                """
                insert into helper_tips (key, route_pattern, module_key, title, body,
                                          cta_label, cta_href, priority, active,
                                          show_to_roles, category)
                values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                body.key, body.route_pattern, body.module_key, body.title, body.body,
                body.cta_label, body.cta_href, body.priority, body.active,
                body.show_to_roles, body.category,
            )
        except Exception as e:
            if "unique" in str(e).lower():
                raise HTTPException(409, "Ya existe un tip con ese key")
            raise
    await audit_admin_action(admin, "helper_tips.create", resource_type="helper_tip",
                             resource_id=body.key, request=request, metadata=body.dict())
    return {"ok": True, "key": body.key}


class TipPatch(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    cta_label: Optional[str] = None
    cta_href: Optional[str] = None
    route_pattern: Optional[str] = None
    module_key: Optional[str] = None
    priority: Optional[int] = None
    active: Optional[bool] = None
    category: Optional[str] = None


@router.patch("/{key}")
async def update_tip(
    key: str, body: TipPatch, request: Request,
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
        result = await conn.execute(
            f"update helper_tips set {', '.join(sets)} where key = $1", *params,
        )
    await audit_admin_action(admin, "helper_tips.update", resource_type="helper_tip",
                             resource_id=key, request=request,
                             metadata={"changes": body.dict(exclude_none=True)})
    return {"ok": True}


@router.delete("/{key}")
async def delete_tip(
    key: str, request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin")),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute("delete from helper_tips where key = $1", key)
    await audit_admin_action(admin, "helper_tips.delete", resource_type="helper_tip",
                             resource_id=key, request=request)
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════
# Welcome emails worker control
# ══════════════════════════════════════════════════════════════════════


welcome_router = APIRouter(prefix="/v1/admin/welcome-emails", tags=["admin-welcome-emails"])


@welcome_router.post("/run")
async def run_worker(
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    """Corre el worker de emails de bienvenida (cron Railway lo invoca)."""
    from agent.workers.welcome_emails import run_welcome_emails
    result = await run_welcome_emails()
    await audit_admin_action(admin, "welcome_emails.run", request=request, metadata=result)
    return result


@welcome_router.get("/log")
async def emails_log(
    limit: int = 100,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    """Listado de emails enviados."""
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select wel.*, f.razon_social as firm_name
              from welcome_emails_log wel
              left join firms f on f.id = wel.firm_id
             order by sent_at desc
             limit $1
            """, limit,
        )
    return {"items": [dict(r) for r in rows]}
