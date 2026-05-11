"""Firm Users API · gestión de miembros de la firma (TASK-S1-06).

Endpoints:
  GET    /v1/firm-users/                       → listar usuarios de la firma
  POST   /v1/firm-users/invite                 → invitar usuario (Supabase Admin)
  PATCH  /v1/firm-users/{user_id}              → cambiar rol/nombre/cédula (admin)
  POST   /v1/firm-users/{user_id}/deactivate   → banear en auth (admin)
  POST   /v1/firm-users/{user_id}/reactivate   → des-banear (admin)

Autorización:
  · `list` y `get` están abiertos a cualquier miembro autenticado de la firma.
  · El resto requiere rol admin / socio_senior / socio_junior.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

import re

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from utils.auth import Principal, get_current_firm

# Conservative email regex (good enough for invites; we don't need RFC-perfect).
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/firm-users", tags=["firm_users"])


# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────

# Roles that can manage other firm users.
ELEVATED_ROLES = {"admin", "socio_senior", "socio_junior"}

# Roles assignable to other users by an admin (anything except readonly/admin
# without explicit promotion).
ASSIGNABLE_ROLES = {
    "admin",
    "socio_senior", "socio_junior",
    "lawyer", "paralegal",
    "independiente", "in_house",
    "funcionario_publico", "consultor",
    "readonly",
}


def _ensure_can_manage(principal: Principal) -> None:
    if principal.role not in ELEVATED_ROLES:
        raise HTTPException(
            403,
            f"Solo socios y admins pueden gestionar usuarios. Tu rol: {principal.role}.",
        )


def _supabase_admin_url(path: str) -> str:
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    if not base:
        raise HTTPException(500, "SUPABASE_URL no configurado")
    return f"{base}/auth/v1{path}"


def _admin_headers() -> dict[str, str]:
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not key:
        raise HTTPException(500, "SUPABASE_SERVICE_ROLE_KEY no configurado")
    return {
        "apikey": key,
        "authorization": f"Bearer {key}",
        "content-type": "application/json",
    }


# ────────────────────────────────────────────────────────────────────
# Schemas
# ────────────────────────────────────────────────────────────────────

class FirmUserRow(BaseModel):
    user_id: str
    email: str
    full_name: str
    role: str
    modo_ejercicio: Optional[str]
    cedula_profesional: Optional[str]
    onboarded_at: Optional[datetime]
    mfa_enrolled: bool = False
    last_login_at: Optional[datetime] = None
    practice_areas: list[str] = Field(default_factory=list)
    is_self: bool = False


class InviteRequest(BaseModel):
    email: str
    full_name: str = Field(min_length=2, max_length=200)
    role: str = Field(default="lawyer")
    modo_ejercicio: Optional[str] = None

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL_RE.match(v):
            raise ValueError("email inválido")
        return v


class InviteResponse(BaseModel):
    user_id: str
    email: str
    invited_at: datetime
    invitation_status: str = "sent"


class FirmUserUpdate(BaseModel):
    full_name: Optional[str] = Field(None, min_length=2, max_length=200)
    role: Optional[str] = None
    modo_ejercicio: Optional[str] = None
    cedula_profesional: Optional[str] = None


# ────────────────────────────────────────────────────────────────────
# Endpoints
# ────────────────────────────────────────────────────────────────────

@router.get("/", response_model=list[FirmUserRow])
async def list_firm_users(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select u.id, u.email, u.full_name,
                   u.role::text as role,
                   u.modo_ejercicio,
                   u.cedula_profesional,
                   u.onboarded_at,
                   u.mfa_enrolled,
                   u.last_login_at,
                   coalesce(
                     (select array_agg(area::text order by is_primary desc, area asc)
                        from user_practice_areas where user_id = u.id),
                     array[]::text[]
                   ) as practice_areas
            from users u
            where u.firm_id = $1::uuid
            order by u.role asc, u.full_name asc
            """,
            principal.firm_id,
        )
    return [
        FirmUserRow(
            user_id=str(r["id"]),
            email=r["email"],
            full_name=r["full_name"],
            role=r["role"],
            modo_ejercicio=r["modo_ejercicio"],
            cedula_profesional=r["cedula_profesional"],
            onboarded_at=r["onboarded_at"],
            mfa_enrolled=bool(r["mfa_enrolled"]),
            last_login_at=r["last_login_at"],
            practice_areas=list(r["practice_areas"] or []),
            is_self=str(r["id"]) == str(principal.user_id),
        )
        for r in rows
    ]


@router.post("/invite", response_model=InviteResponse, status_code=201)
async def invite_user(
    body: InviteRequest,
    principal: Principal = Depends(get_current_firm),
):
    _ensure_can_manage(principal)
    if body.role not in ASSIGNABLE_ROLES:
        raise HTTPException(400, f"role inválido: {body.role}")

    # 1. Send Supabase invite email (creates auth.users row in invited state).
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _supabase_admin_url("/admin/invite"),
            headers=_admin_headers(),
            json={
                "email": str(body.email),
                "data": {
                    "full_name": body.full_name,
                    "invited_by_firm_id": principal.firm_id,
                    "invited_by_user_id": principal.user_id,
                },
            },
        )
        if resp.status_code >= 400:
            detail = resp.text[:300]
            logger.warning("supabase invite failed: %s %s", resp.status_code, detail)
            raise HTTPException(resp.status_code, f"Error invitando: {detail}")
        auth_user = resp.json()
    new_user_id = auth_user.get("id") or auth_user.get("user", {}).get("id")
    if not new_user_id:
        raise HTTPException(502, "Supabase no retornó user_id")

    # 2. Create / upsert profile row in public.users with desired role + firm.
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            insert into users (id, firm_id, email, full_name, role)
            values ($1::uuid, $2::uuid, $3, $4, $5::user_role)
            on conflict (id) do update
              set firm_id = excluded.firm_id,
                  email = excluded.email,
                  full_name = excluded.full_name,
                  role = excluded.role,
                  updated_at = now()
            """,
            new_user_id,
            principal.firm_id,
            str(body.email),
            body.full_name,
            body.role,
        )
        if body.modo_ejercicio:
            await conn.execute(
                "update users set modo_ejercicio = $1 where id = $2::uuid",
                body.modo_ejercicio, new_user_id,
            )

    return InviteResponse(
        user_id=str(new_user_id),
        email=str(body.email),
        invited_at=datetime.utcnow(),
        invitation_status="sent",
    )


@router.patch("/{user_id}", response_model=FirmUserRow)
async def update_firm_user(
    user_id: str,
    body: FirmUserUpdate,
    principal: Principal = Depends(get_current_firm),
):
    _ensure_can_manage(principal)
    if body.role is not None and body.role not in ASSIGNABLE_ROLES:
        raise HTTPException(400, f"role inválido: {body.role}")
    # Prevent demoting yourself by accident if you're the only admin.
    if user_id == principal.user_id and body.role and body.role != principal.role:
        # Soft warn only; allow it. (Future: enforce "at least one admin".)
        pass

    fields, params = [], []
    if body.full_name is not None:
        params.append(body.full_name)
        fields.append(f"full_name = ${len(params)}")
    if body.role is not None:
        params.append(body.role)
        fields.append(f"role = ${len(params)}::user_role")
    if body.modo_ejercicio is not None:
        params.append(body.modo_ejercicio)
        fields.append(f"modo_ejercicio = ${len(params)}")
    if body.cedula_profesional is not None:
        params.append(body.cedula_profesional)
        fields.append(f"cedula_profesional = ${len(params)}")
    if not fields:
        raise HTTPException(400, "nada que actualizar")
    fields.append("updated_at = now()")
    params.append(user_id)
    params.append(principal.firm_id)
    sql = f"update users set {', '.join(fields)} where id = ${len(params) - 1}::uuid and firm_id = ${len(params)}::uuid"

    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        result = await conn.execute(sql, *params)
        if result.endswith(" 0"):
            raise HTTPException(404, "usuario no encontrado en esta firma")

    # Re-read to return fresh state.
    return await _get_one(principal, user_id)


@router.post("/{user_id}/deactivate", status_code=204)
async def deactivate_user(
    user_id: str,
    principal: Principal = Depends(get_current_firm),
):
    _ensure_can_manage(principal)
    if user_id == principal.user_id:
        raise HTTPException(400, "No puedes desactivarte a ti mismo.")
    await _set_ban(user_id, principal.firm_id, banned=True)


@router.post("/{user_id}/reactivate", status_code=204)
async def reactivate_user(
    user_id: str,
    principal: Principal = Depends(get_current_firm),
):
    _ensure_can_manage(principal)
    await _set_ban(user_id, principal.firm_id, banned=False)


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────

async def _get_one(principal: Principal, user_id: str) -> FirmUserRow:
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            select u.id, u.email, u.full_name,
                   u.role::text as role,
                   u.modo_ejercicio,
                   u.cedula_profesional,
                   u.onboarded_at,
                   u.mfa_enrolled,
                   u.last_login_at,
                   coalesce(
                     (select array_agg(area::text order by is_primary desc, area asc)
                        from user_practice_areas where user_id = u.id),
                     array[]::text[]
                   ) as practice_areas
            from users u
            where u.id = $1::uuid and u.firm_id = $2::uuid
            """,
            user_id, principal.firm_id,
        )
    if not r:
        raise HTTPException(404, "usuario no encontrado")
    return FirmUserRow(
        user_id=str(r["id"]),
        email=r["email"],
        full_name=r["full_name"],
        role=r["role"],
        modo_ejercicio=r["modo_ejercicio"],
        cedula_profesional=r["cedula_profesional"],
        onboarded_at=r["onboarded_at"],
        mfa_enrolled=bool(r["mfa_enrolled"]),
        last_login_at=r["last_login_at"],
        practice_areas=list(r["practice_areas"] or []),
        is_self=str(r["id"]) == str(principal.user_id),
    )


async def _set_ban(user_id: str, firm_id: str, *, banned: bool) -> None:
    """Ban or un-ban a user via Supabase Admin API. Validates firm membership
    before performing the op (defense in depth: the public.users row must
    exist and belong to the requester's firm)."""
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        member = await conn.fetchrow(
            "select id from users where id = $1::uuid and firm_id = $2::uuid",
            user_id, firm_id,
        )
    if not member:
        raise HTTPException(404, "usuario no pertenece a esta firma")

    payload = {"ban_duration": "876000h" if banned else "none"}  # 100 years effective
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(
            _supabase_admin_url(f"/admin/users/{user_id}"),
            headers=_admin_headers(),
            json=payload,
        )
        if resp.status_code >= 400:
            raise HTTPException(resp.status_code, f"Error en ban/unban: {resp.text[:200]}")
