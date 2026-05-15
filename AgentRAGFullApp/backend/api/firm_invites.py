"""Sprint 29 · Firm invites · códigos para que nuevos usuarios se unan a una firma existente.

Endpoints (firm-scoped):
  GET    /v1/me/firm-invites           · listar códigos activos de mi firma
  POST   /v1/me/firm-invites           · generar nuevo código
  DELETE /v1/me/firm-invites/{id}      · revocar código

Endpoint público (autenticado pero sin firm_id):
  POST   /v1/me/join-firm              · usar código de invitación · une al user a la firm
  POST   /v1/me/create-firm            · crear nueva firma (no tiene invite, signup nuevo)
"""

from __future__ import annotations

import logging
import secrets
import string
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_user, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/me", tags=["firm-invites"])


def _generate_code() -> str:
    """Genera código tipo LEXAI-XXXX-YYYY (8 chars + dash)."""
    alphabet = string.ascii_uppercase + string.digits
    p1 = ''.join(secrets.choice(alphabet) for _ in range(4))
    p2 = ''.join(secrets.choice(alphabet) for _ in range(4))
    return f"{p1}-{p2}"


# ══════════════════════════════════════════════════════════════════════
# Firm admin: generar y gestionar códigos
# ══════════════════════════════════════════════════════════════════════


@router.get("/firm-invites")
async def list_invites(principal: Principal = Depends(get_current_firm)):
    if principal.role not in ('admin', 'socio_senior', 'socio_junior', 'in_house', 'independiente'):
        raise HTTPException(403, "Solo admin/socios pueden gestionar invitaciones")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, code, role_to_assign, max_uses, used_count,
                   expires_at, created_at, metadata
              from firm_invite_codes
             where firm_id = $1::uuid
             order by created_at desc
            """, principal.firm_id,
        )
    return {"items": [dict(r) for r in rows]}


class InviteCreate(BaseModel):
    role_to_assign: str = Field(default="lawyer",
                                 pattern="^(lawyer|paralegal|secretary|socio_junior|admin)$")
    max_uses: int = Field(default=10, ge=1, le=1000)
    expires_in_days: int = Field(default=30, ge=1, le=365)


@router.post("/firm-invites")
async def create_invite(
    body: InviteCreate,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ('admin', 'socio_senior', 'socio_junior', 'in_house', 'independiente'):
        raise HTTPException(403, "Solo admin/socios pueden generar invitaciones")
    code = _generate_code()
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into firm_invite_codes
              (firm_id, code, role_to_assign, max_uses, expires_at, created_by)
            values ($1::uuid, $2, $3, $4, now() + ($5 || ' days')::interval, $6::uuid)
            returning id, code, role_to_assign, max_uses, expires_at, created_at
            """,
            principal.firm_id, code, body.role_to_assign, body.max_uses,
            str(body.expires_in_days), principal.user_id,
        )
    return dict(row)


@router.delete("/firm-invites/{invite_id}")
async def revoke_invite(
    invite_id: str,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ('admin', 'socio_senior', 'socio_junior', 'in_house', 'independiente'):
        raise HTTPException(403, "Solo admin/socios pueden revocar invitaciones")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "delete from firm_invite_codes where id = $1::uuid and firm_id = $2::uuid",
            invite_id, principal.firm_id,
        )
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════
# Nuevo usuario: redimir invite OR crear firma
# Usado desde el wizard onboarding después de Google OAuth signup
# ══════════════════════════════════════════════════════════════════════


class JoinFirmBody(BaseModel):
    code: str = Field(min_length=4, max_length=32)


@router.post("/join-firm")
async def join_firm(
    body: JoinFirmBody,
    principal: Principal = Depends(get_current_user),  # NO get_current_firm · user puede no tener firm aún
):
    """Redime código de invitación · asigna firm_id + role al user actual."""
    import json as _json
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        result = await conn.fetchval(
            "select lexai_redeem_invite_code($1, $2::uuid)",
            body.code.strip().upper(), principal.user_id,
        )
    if isinstance(result, str):
        try:
            result = _json.loads(result)
        except Exception:
            result = None
    if not result or not result.get('ok'):
        raise HTTPException(
            400,
            detail={
                "error": result.get('error', 'unknown') if result else 'no_result',
                "message": (
                    "Código inválido o expirado" if result and result.get('error') == 'invalid_or_expired_code'
                    else "Ya perteneces a una firma · cierra sesión y vuelve a ingresar"
                    if result and result.get('error') == 'user_already_in_firm'
                    else "No se pudo redimir el código"
                ),
            },
        )
    return result


class CreateFirmBody(BaseModel):
    razon_social: str = Field(min_length=2, max_length=200)
    country: str = Field(default="co", pattern="^(co|mx|hn|gt)$")
    tax_id: Optional[str] = None
    modo_ejercicio: Optional[str] = Field(default=None,
                                            pattern="^(independiente|firma|in_house|sector_publico|consultoria)?$")
    role: str = Field(default="independiente",
                       pattern="^(independiente|admin|socio_senior|socio_junior|in_house|consultor|lawyer)$")


@router.post("/create-firm")
async def create_firm(
    body: CreateFirmBody,
    principal: Principal = Depends(get_current_user),
):
    """Crea firma nueva + asigna firm_id al user actual.

    Los triggers Sprint 23 (auto plan free) y Sprint 26 (seed demo data)
    se disparan automáticamente al INSERT en firms.
    """
    import json as _json
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        result = await conn.fetchval(
            "select lexai_create_firm_for_user($1::uuid, $2, $3, $4, $5, $6)",
            principal.user_id, body.razon_social, body.country, body.tax_id,
            body.modo_ejercicio, body.role,
        )
    # asyncpg puede devolver jsonb como str · normalizar
    if isinstance(result, str):
        try:
            result = _json.loads(result)
        except Exception:
            result = None
    if not result or not result.get('ok'):
        raise HTTPException(400, detail=result or "No se pudo crear la firma")
    return result
