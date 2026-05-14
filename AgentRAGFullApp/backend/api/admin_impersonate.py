"""Sprint 24 · SaaS Admin · Impersonation (read-only).

Endpoints:
  POST /v1/admin/impersonate         · genera token impersonation (15 min TTL)
  POST /v1/admin/impersonate/revoke  · revoca un token activo
  GET  /v1/admin/impersonate/active  · lista impersonaciones activas

Diseño:
  - Genera un token corto-vivido firmado con el secret del backend (NO JWT Supabase).
  - El token contiene: admin_user_id, target_firm_id, target_user_id, exp, jti.
  - Endpoints normales NO aceptan este token (los endpoints del cliente siguen
    usando JWT Supabase normal). Este token solo sirve para abrir un endpoint
    auxiliar `/v1/admin/impersonate/preview/...` que devuelve el estado de
    la firm como la vería el usuario.
  - Toda impersonación queda en audit_logs con scope='saas_admin' +
    metadata.impersonation=true.

Para MVP · solo soportamos "vista previa" del estado, NO ejecutar acciones
como otro usuario. Esto reduce mucho el riesgo de abuso.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from utils.admin_guard import (
    AdminPrincipal, require_saas_admin, require_saas_admin_role, audit_admin_action,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/admin/impersonate", tags=["admin-impersonate"])

TTL_SECONDS = 15 * 60  # 15 min


def _secret() -> str:
    s = os.getenv("LEXAI_IMPERSONATION_SECRET") or os.getenv("SUPABASE_JWT_SECRET")
    if not s:
        # Fallback dev · no usar en prod
        return "lexai-dev-impersonation-do-not-use-in-prod"
    return s


def _sign(payload: str) -> str:
    return hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()


def _make_token(admin_user_id: str, firm_id: str, user_id: Optional[str] = None) -> dict:
    jti = secrets.token_urlsafe(12)
    exp = int(time.time()) + TTL_SECONDS
    payload = f"{admin_user_id}:{firm_id}:{user_id or ''}:{exp}:{jti}"
    sig = _sign(payload)
    return {"token": f"{payload}::{sig}", "exp": exp, "jti": jti}


def _verify_token(token: str) -> dict:
    try:
        payload, sig = token.rsplit("::", 1)
        expected = _sign(payload)
        if not hmac.compare_digest(expected, sig):
            raise ValueError("bad signature")
        admin_id, firm_id, user_id, exp_str, jti = payload.split(":")
        if int(exp_str) < int(time.time()):
            raise ValueError("expired")
        return {
            "admin_user_id": admin_id,
            "firm_id": firm_id,
            "user_id": user_id or None,
            "exp": int(exp_str),
            "jti": jti,
        }
    except Exception as e:
        raise HTTPException(401, f"invalid impersonation token: {e}")


class ImpersonateBody(BaseModel):
    firm_id: str
    user_id: Optional[str] = None
    reason: str = Field(min_length=10, max_length=500)


@router.post("")
async def impersonate(
    body: ImpersonateBody,
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin", "support")),
):
    """Crea token de impersonation read-only para una firm específica.

    Audita siempre · marca firm_id objetivo + reason.
    """
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        firm = await conn.fetchrow("select id, razon_social from firms where id = $1::uuid", body.firm_id)
    if not firm:
        raise HTTPException(404, "firm not found")
    tok = _make_token(admin.admin_user_id, body.firm_id, body.user_id)
    await audit_admin_action(admin, "impersonate.start", resource_type="firm",
                             resource_id=body.firm_id, firm_id=body.firm_id, request=request,
                             reason=body.reason,
                             metadata={"impersonation": True, "jti": tok["jti"],
                                       "ttl_seconds": TTL_SECONDS, "user_id": body.user_id})
    return {
        "token": tok["token"],
        "exp": tok["exp"],
        "firm": {"id": str(firm["id"]), "razon_social": firm["razon_social"]},
        "ttl_seconds": TTL_SECONDS,
    }


@router.get("/preview")
async def impersonate_preview(
    request: Request,
    token: str,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    """Devuelve el estado de la firm como la vería el usuario (read-only).
    Requiere ambos: admin JWT y token de impersonation válido."""
    payload = _verify_token(token)
    if payload["admin_user_id"] != admin.admin_user_id:
        raise HTTPException(403, "token no pertenece a este admin")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        firm = await conn.fetchrow("select * from firms where id = $1::uuid", payload["firm_id"])
        health = await conn.fetchval("select lexai_saas_firm_health($1::uuid)", payload["firm_id"])
        recent_matters = await conn.fetch(
            "select id, titulo, status, created_at from matters where firm_id = $1::uuid "
            "order by created_at desc limit 10",
            payload["firm_id"],
        )
        quota = await conn.fetchval("select lexai_quota_status($1::uuid)", payload["firm_id"])
    await audit_admin_action(admin, "impersonate.preview", resource_type="firm",
                             resource_id=payload["firm_id"], firm_id=payload["firm_id"],
                             request=request,
                             metadata={"impersonation": True, "jti": payload["jti"]})
    return {
        "firm": dict(firm) if firm else None,
        "health": health,
        "quota_status": quota,
        "recent_matters": [dict(r) for r in recent_matters],
    }


@router.post("/revoke")
async def impersonate_revoke(
    request: Request,
    token: str,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    """Revoca explícitamente un token (lo deja en audit_logs como invalidated).

    Nota: el token aún caducará por TTL · esto es solo simbólico/auditoría.
    """
    payload = _verify_token(token)
    await audit_admin_action(admin, "impersonate.revoke", resource_type="firm",
                             resource_id=payload["firm_id"], firm_id=payload["firm_id"],
                             request=request,
                             metadata={"impersonation": True, "jti": payload["jti"], "revoked": True})
    return {"ok": True}
