"""Sprint 24 · SaaS Admin · Users management cross-firm.

Endpoints:
  GET   /v1/admin/users                       · listado cross-firm + filtros
  GET   /v1/admin/users/{user_id}             · detalle
  POST  /v1/admin/users/{user_id}/reset-password   · envía email reset via Supabase admin API
  POST  /v1/admin/users/{user_id}/disable     · soft-disable (mfa unset, last_login null)
  POST  /v1/admin/users/{user_id}/role        · cambiar role

  GET   /v1/admin/admin-users                 · gestionar admin_users (owner only)
  POST  /v1/admin/admin-users                 · crear admin (owner only)
  PATCH /v1/admin/admin-users/{id}            · update admin (owner only)
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from utils.admin_guard import (
    AdminPrincipal, require_saas_admin, require_saas_admin_role, audit_admin_action,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/admin", tags=["admin-users"])


@router.get("/users")
async def list_users(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    firm_id: Optional[str] = None,
    role: Optional[str] = None,
    q: Optional[str] = None,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    where = ["1=1"]
    params: list = []
    if firm_id:
        params.append(firm_id); where.append(f"u.firm_id = ${len(params)}::uuid")
    if role:
        params.append(role); where.append(f"u.role = ${len(params)}")
    if q:
        params.append(f"%{q.lower()}%")
        where.append(f"(lower(u.email) like ${len(params)} or lower(u.full_name) like ${len(params)})")
    params.extend([limit, offset])
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            select u.id, u.email, u.full_name, u.role, u.cedula_profesional,
                   u.firm_id, u.mfa_enrolled, u.last_login_at, u.created_at,
                   f.razon_social as firm_name
              from users u
              left join firms f on f.id = u.firm_id
             where {' and '.join(where)}
             order by u.created_at desc
             limit ${len(params) - 1} offset ${len(params)}
            """, *params,
        )
        total = await conn.fetchval("select count(*) from users")
    return {"items": [dict(r) for r in rows], "total": total or 0, "limit": limit, "offset": offset}


@router.get("/users/{user_id}")
async def user_detail(
    user_id: str,
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select u.*, f.razon_social as firm_name
              from users u
              left join firms f on f.id = u.firm_id
             where u.id = $1::uuid
            """, user_id,
        )
        if not row:
            raise HTTPException(404, "user not found")
        recent = await conn.fetch(
            """
            select action, resource_type, resource_id, occurred_at, outcome
              from audit_logs
             where user_id = $1::uuid
             order by occurred_at desc limit 30
            """, user_id,
        )
    return {"user": dict(row), "recent_activity": [dict(r) for r in recent]}


@router.post("/users/{user_id}/reset-password")
async def user_reset_password(
    user_id: str,
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin", "support")),
):
    """Dispara email de recovery vía Supabase Auth Admin API."""
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        user = await conn.fetchrow("select id, email from users where id = $1::uuid", user_id)
        if not user:
            raise HTTPException(404, "user not found")

    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not (supabase_url and service_key):
        # Fallback: solo audit, no enviamos email
        await audit_admin_action(admin, "users.reset_password.attempted",
                                 resource_type="user", resource_id=user_id, request=request,
                                 outcome="error", reason="Supabase admin keys not configured")
        return {"ok": False, "message": "Supabase admin no configurado · solo se registró la auditoría"}

    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            f"{supabase_url}/auth/v1/recover",
            headers={"apikey": service_key, "Authorization": f"Bearer {service_key}",
                     "Content-Type": "application/json"},
            json={"email": user["email"]},
        )
    ok = r.status_code in (200, 201, 204)
    await audit_admin_action(admin, "users.reset_password", resource_type="user",
                             resource_id=user_id, request=request,
                             outcome="success" if ok else "error",
                             metadata={"email": user["email"], "status": r.status_code})
    return {"ok": ok, "email": user["email"]}


class RoleChange(BaseModel):
    role: str = Field(pattern="^(lawyer|paralegal|secretary|admin|owner)$")


@router.post("/users/{user_id}/role")
async def user_change_role(
    user_id: str,
    body: RoleChange,
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin")),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "update users set role = $2, updated_at = now() where id = $1::uuid",
            user_id, body.role,
        )
    await audit_admin_action(admin, "users.role_change", resource_type="user",
                             resource_id=user_id, request=request,
                             metadata={"new_role": body.role})
    return {"ok": True}


# ── admin_users (owner-only) ────────────────────────────────────────────


@router.get("/admin-users")
async def list_admin_users(
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner")),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            "select id, auth_user_id, email, full_name, role, active, last_login_at, created_at "
            "from admin_users order by created_at desc",
        )
    return {"items": [dict(r) for r in rows]}


class AdminUserCreate(BaseModel):
    email: EmailStr
    full_name: Optional[str] = None
    role: str = Field(default="admin", pattern="^(owner|admin|support|readonly)$")
    auth_user_id: Optional[str] = None  # opcional: si ya existe en Supabase auth


@router.post("/admin-users")
async def create_admin_user(
    body: AdminUserCreate,
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner")),
):
    from utils.db import get_storage
    storage = await get_storage()
    # Si no envían auth_user_id, ponemos un placeholder (el admin completará en su primer login).
    # En primer login con el email correcto, el bootstrap path lo enlaza.
    placeholder = body.auth_user_id or "00000000-0000-0000-0000-000000000000"
    async with storage.pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                insert into admin_users (auth_user_id, email, full_name, role, created_by)
                values ($1::uuid, $2, $3, $4, $5::uuid)
                returning id, email, role, created_at
                """,
                placeholder, body.email.lower(), body.full_name, body.role, admin.admin_user_id,
            )
        except Exception as e:
            if "unique" in str(e).lower():
                raise HTTPException(409, "Ese email ya está registrado como admin")
            raise
    await audit_admin_action(admin, "admin_users.create", resource_type="admin_user",
                             resource_id=str(row["id"]), request=request,
                             metadata={"email": body.email, "role": body.role})
    return dict(row)


class AdminUserPatch(BaseModel):
    role: Optional[str] = Field(default=None, pattern="^(owner|admin|support|readonly)$")
    active: Optional[bool] = None
    full_name: Optional[str] = None


@router.patch("/admin-users/{admin_id}")
async def update_admin_user(
    admin_id: str,
    body: AdminUserPatch,
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner")),
):
    if admin_id == admin.admin_user_id and body.active is False:
        raise HTTPException(400, "No puedes desactivarte a ti mismo")
    sets: list[str] = []
    params: list = [admin_id]
    for field, value in body.dict(exclude_none=True).items():
        params.append(value); sets.append(f"{field} = ${len(params)}")
    if not sets:
        raise HTTPException(400, "Nada que actualizar")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            f"update admin_users set {', '.join(sets)} where id = $1::uuid",
            *params,
        )
    await audit_admin_action(admin, "admin_users.update", resource_type="admin_user",
                             resource_id=admin_id, request=request,
                             metadata={"changes": body.dict(exclude_none=True)})
    return {"ok": True}


@router.get("/me")
async def whoami(admin: AdminPrincipal = Depends(require_saas_admin)):
    """Quién soy yo en el panel admin. Útil para gates de UI."""
    return {
        "admin_user_id": admin.admin_user_id,
        "email": admin.email,
        "full_name": admin.full_name,
        "role": admin.role,
    }
