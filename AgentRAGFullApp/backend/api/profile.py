"""User Profile API · perfil del usuario actual (Sprint 1).

Endpoints para que el usuario gestione SU PROPIO perfil:
  GET   /v1/profile/me            → datos completos (rol, modo, áreas, onboarded)
  PATCH /v1/profile/me            → actualiza modo_ejercicio, role, full_name, etc.
  GET   /v1/profile/me/areas      → áreas de práctica del usuario
  PUT   /v1/profile/me/areas      → reemplaza el set completo de áreas
  POST  /v1/profile/me/onboard    → marca onboarded_at + commit del wizard

Multi-tenant: el usuario solo puede ver/modificar SU propia row.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/profile", tags=["profile"])


# Allowed modos de ejercicio (mirror of CHECK constraint in users.modo_ejercicio).
ALLOWED_MODES = {"independiente", "firma", "in_house", "sector_publico", "consultoria"}

# Allowed roles (subset of user_role enum that the user can self-assign in
# onboarding). `admin` is NOT in this set: it must be granted by another
# admin via the firm-users management endpoints, never by self-promotion.
# A user who already IS admin can keep being admin (handled per-request
# below in update_me / onboard).
SELF_ASSIGNABLE_ROLES = {
    "lawyer", "paralegal",
    "socio_senior", "socio_junior",
    "independiente", "in_house",
    "funcionario_publico", "consultor",
}


# ────────────────────────────────────────────────────────────────────
# Schemas
# ────────────────────────────────────────────────────────────────────

class ProfileResponse(BaseModel):
    user_id: str
    firm_id: str
    email: str
    full_name: str
    role: str
    modo_ejercicio: Optional[str]
    onboarded_at: Optional[datetime]
    cedula_profesional: Optional[str]
    practice_areas: list[str] = Field(default_factory=list)
    primary_area: Optional[str] = None


class ProfileUpdate(BaseModel):
    full_name: Optional[str] = Field(None, min_length=2, max_length=200)
    modo_ejercicio: Optional[str] = None
    role: Optional[str] = None
    cedula_profesional: Optional[str] = None


class AreasUpdate(BaseModel):
    areas: list[str] = Field(default_factory=list, max_length=11)
    primary_area: Optional[str] = None


class OnboardRequest(BaseModel):
    modo_ejercicio: str
    role: str
    practice_areas: list[str] = Field(default_factory=list, max_length=11)
    primary_area: Optional[str] = None
    full_name: Optional[str] = None
    cedula_profesional: Optional[str] = None


# ────────────────────────────────────────────────────────────────────
# Endpoints
# ────────────────────────────────────────────────────────────────────

@router.get("/me", response_model=ProfileResponse)
async def get_me(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, firm_id, email, full_name, role::text as role,
                   modo_ejercicio, onboarded_at, cedula_profesional
            from users where id = $1::uuid
            """,
            principal.user_id,
        )
        if not row:
            raise HTTPException(404, "perfil no encontrado")
        areas = await conn.fetch(
            """
            select area::text as area, is_primary
            from user_practice_areas
            where user_id = $1::uuid
            order by is_primary desc, area asc
            """,
            principal.user_id,
        )
    primary = next((a["area"] for a in areas if a["is_primary"]), None)
    return ProfileResponse(
        user_id=str(row["id"]),
        firm_id=str(row["firm_id"]),
        email=row["email"],
        full_name=row["full_name"],
        role=row["role"],
        modo_ejercicio=row["modo_ejercicio"],
        onboarded_at=row["onboarded_at"],
        cedula_profesional=row["cedula_profesional"],
        practice_areas=[a["area"] for a in areas],
        primary_area=primary,
    )


@router.patch("/me", response_model=ProfileResponse)
async def update_me(
    body: ProfileUpdate,
    principal: Principal = Depends(get_current_firm),
):
    if body.modo_ejercicio is not None and body.modo_ejercicio not in ALLOWED_MODES:
        raise HTTPException(400, f"modo_ejercicio inválido: {body.modo_ejercicio}")
    # Role guard: a user can keep their CURRENT role (idempotent saves)
    # but cannot self-promote to an elevated role like 'admin'.
    if body.role is not None and body.role not in SELF_ASSIGNABLE_ROLES:
        if body.role != principal.role:
            raise HTTPException(
                400,
                f"role no auto-asignable: {body.role}. "
                "Solo otro admin puede promover a este rol.",
            )

    fields, params = [], []
    if body.full_name is not None:
        params.append(body.full_name)
        fields.append(f"full_name = ${len(params)}")
    if body.modo_ejercicio is not None:
        params.append(body.modo_ejercicio)
        fields.append(f"modo_ejercicio = ${len(params)}")
    if body.role is not None:
        params.append(body.role)
        fields.append(f"role = ${len(params)}::user_role")
    if body.cedula_profesional is not None:
        params.append(body.cedula_profesional)
        fields.append(f"cedula_profesional = ${len(params)}")
    if not fields:
        raise HTTPException(400, "nada que actualizar")
    fields.append("updated_at = now()")
    params.append(principal.user_id)
    sql = f"update users set {', '.join(fields)} where id = ${len(params)}::uuid"
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(sql, *params)
    return await get_me(principal)


@router.put("/me/areas", response_model=ProfileResponse)
async def replace_areas(
    body: AreasUpdate,
    principal: Principal = Depends(get_current_firm),
):
    if body.primary_area and body.primary_area not in body.areas:
        raise HTTPException(400, "primary_area debe estar dentro de areas")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "delete from user_practice_areas where user_id = $1::uuid",
                principal.user_id,
            )
            for area in body.areas:
                await conn.execute(
                    """
                    insert into user_practice_areas (user_id, area, is_primary)
                    values ($1::uuid, $2::materia_legal, $3)
                    on conflict (user_id, area) do update
                      set is_primary = excluded.is_primary
                    """,
                    principal.user_id, area, area == body.primary_area,
                )
    return await get_me(principal)


@router.post("/me/onboard", response_model=ProfileResponse)
async def onboard(
    body: OnboardRequest,
    principal: Principal = Depends(get_current_firm),
):
    if body.modo_ejercicio not in ALLOWED_MODES:
        raise HTTPException(400, f"modo_ejercicio inválido: {body.modo_ejercicio}")
    # Same idempotent-keep semantics as update_me: a user finishing
    # onboarding while already being admin keeps that role.
    if body.role not in SELF_ASSIGNABLE_ROLES and body.role != principal.role:
        raise HTTPException(400, f"role no auto-asignable: {body.role}")
    if body.primary_area and body.primary_area not in body.practice_areas:
        raise HTTPException(400, "primary_area debe estar dentro de practice_areas")

    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        async with conn.transaction():
            # Update user core fields and mark onboarded.
            await conn.execute(
                """
                update users set
                  modo_ejercicio = $1,
                  role = $2::user_role,
                  full_name = coalesce($3, full_name),
                  cedula_profesional = coalesce($4, cedula_profesional),
                  onboarded_at = now(),
                  updated_at = now()
                where id = $5::uuid
                """,
                body.modo_ejercicio,
                body.role,
                body.full_name,
                body.cedula_profesional,
                principal.user_id,
            )
            # Replace practice areas.
            await conn.execute(
                "delete from user_practice_areas where user_id = $1::uuid",
                principal.user_id,
            )
            for area in body.practice_areas:
                await conn.execute(
                    """
                    insert into user_practice_areas (user_id, area, is_primary)
                    values ($1::uuid, $2::materia_legal, $3)
                    """,
                    principal.user_id, area, area == body.primary_area,
                )
    return await get_me(principal)
