"""Sprint 5 · Email integrations API.

Permite a los usuarios conectar cuentas Gmail / Outlook / IMAP para que LexAI
lea correos legales (notificaciones del juzgado, traslados, oficios) y los
clasifique en el inbox unificado.

Endpoints:
  GET    /v1/email/integrations                 → lista cuentas conectadas
  POST   /v1/email/integrations                 → crea (manual / IMAP / pending OAuth)
  PATCH  /v1/email/integrations/{id}            → activar/desactivar/filtros
  DELETE /v1/email/integrations/{id}            → desconectar
  POST   /v1/email/integrations/{id}/test       → ping conectividad
  GET    /v1/email/oauth/{provider}/start       → URL OAuth (stub)
  GET    /v1/email/oauth/{provider}/callback    → maneja code (stub · marca status='connected')

OAuth real (Gmail / Outlook) requiere credenciales de app:
  GMAIL_OAUTH_CLIENT_ID / GMAIL_OAUTH_CLIENT_SECRET
  OUTLOOK_OAUTH_CLIENT_ID / OUTLOOK_OAUTH_CLIENT_SECRET
Si no están seteadas, los endpoints OAuth devuelven un mensaje útil pero
no bloquean el resto de la API. El admin puede crear integraciones IMAP
o "manual" (placeholder) sin OAuth.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/email", tags=["email_integrations"])


GMAIL_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.metadata",
]
OUTLOOK_AUTH_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
OUTLOOK_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
OUTLOOK_SCOPES = ["offline_access", "Mail.Read", "Mail.ReadBasic"]


class CreateIntegrationRequest(BaseModel):
    provider: str = Field(pattern="^(gmail|outlook|imap)$")
    email_address: str = Field(min_length=3, max_length=320)
    display_name: Optional[str] = None
    imap_host: Optional[str] = None
    imap_port: Optional[int] = None
    imap_username: Optional[str] = None
    imap_password: Optional[str] = None
    watch_label: Optional[str] = "INBOX"
    filter_query: Optional[str] = None


class PatchIntegrationRequest(BaseModel):
    active: Optional[bool] = None
    display_name: Optional[str] = None
    watch_label: Optional[str] = None
    filter_query: Optional[str] = None


@router.get("/integrations")
async def list_integrations(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, user_id, provider, email_address, display_name,
                   active, status, last_status, last_error, last_synced_at,
                   watch_label, filter_query, created_at, updated_at
              from email_integrations
             where firm_id = $1::uuid
             order by created_at desc
            """,
            principal.firm_id,
        )
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]),
                "user_id": str(r["user_id"]),
                "provider": r["provider"],
                "email_address": r["email_address"],
                "display_name": r["display_name"],
                "active": r["active"],
                "status": r["status"],
                "last_status": r["last_status"],
                "last_error": r["last_error"],
                "last_synced_at": r["last_synced_at"].isoformat() if r["last_synced_at"] else None,
                "watch_label": r["watch_label"],
                "filter_query": r["filter_query"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ],
    }


@router.post("/integrations")
async def create_integration(
    body: CreateIntegrationRequest,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    initial_status = "pending"
    if body.provider == "imap":
        # Para IMAP, asumimos credenciales válidas hasta que el primer sync falle.
        if not (body.imap_host and body.imap_username and body.imap_password):
            raise HTTPException(400, "imap requiere host, username, password")
        initial_status = "connected"
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into email_integrations
              (firm_id, user_id, provider, email_address, display_name,
               imap_host, imap_port, imap_username, imap_password_enc,
               watch_label, filter_query, status)
            values ($1::uuid, $2::uuid, $3, $4, $5,
                    $6, $7, $8, $9, $10, $11, $12)
            on conflict (user_id, provider, email_address) do update set
              display_name = excluded.display_name,
              imap_host = excluded.imap_host,
              imap_port = excluded.imap_port,
              imap_username = excluded.imap_username,
              imap_password_enc = excluded.imap_password_enc,
              watch_label = excluded.watch_label,
              filter_query = excluded.filter_query,
              active = true,
              updated_at = now()
            returning id, status, provider
            """,
            principal.firm_id, principal.user_id, body.provider,
            body.email_address.lower(), body.display_name,
            body.imap_host, body.imap_port, body.imap_username,
            body.imap_password,  # NOTE: en Sprint 6 cifrar con KMS
            body.watch_label, body.filter_query, initial_status,
        )
    return {
        "id": str(row["id"]),
        "provider": row["provider"],
        "status": row["status"],
        "next_step": (
            "Visita /v1/email/oauth/{provider}/start para autorizar acceso"
            if body.provider in ("gmail", "outlook")
            else "Conexión IMAP activa. El sync inicial corre en próximo poll."
        ),
    }


@router.patch("/integrations/{integration_id}")
async def patch_integration(
    integration_id: str,
    body: PatchIntegrationRequest,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")

    fields = []
    params: list = [integration_id, principal.firm_id]
    if body.active is not None:
        params.append(body.active)
        fields.append(f"active = ${len(params)}")
    if body.display_name is not None:
        params.append(body.display_name)
        fields.append(f"display_name = ${len(params)}")
    if body.watch_label is not None:
        params.append(body.watch_label)
        fields.append(f"watch_label = ${len(params)}")
    if body.filter_query is not None:
        params.append(body.filter_query)
        fields.append(f"filter_query = ${len(params)}")
    if not fields:
        raise HTTPException(400, "no fields to update")

    sql = f"""
        update email_integrations set {', '.join(fields)}, updated_at = now()
         where id = $1::uuid and firm_id = $2::uuid
        returning id, active, status
    """
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(sql, *params)
    if not row:
        raise HTTPException(404, "not found")
    return {"id": str(row["id"]), "active": row["active"], "status": row["status"]}


@router.delete("/integrations/{integration_id}")
async def delete_integration(
    integration_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        rows = await conn.execute(
            "delete from email_integrations where id = $1::uuid and firm_id = $2::uuid",
            integration_id, principal.firm_id,
        )
    return {"deleted": True, "rows": rows}


@router.post("/integrations/{integration_id}/test")
async def test_integration(
    integration_id: str,
    principal: Principal = Depends(get_current_firm),
):
    """Ping de conectividad. Real implementation will hit Gmail/Outlook API.
    Por ahora devuelve un OK informativo."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select provider, status, oauth_access_token, imap_host, email_address
              from email_integrations
             where id = $1::uuid and firm_id = $2::uuid
            """,
            integration_id, principal.firm_id,
        )
    if not row:
        raise HTTPException(404, "not found")
    if row["provider"] in ("gmail", "outlook"):
        ok = bool(row["oauth_access_token"])
    else:
        ok = bool(row["imap_host"])
    return {
        "ok": ok,
        "provider": row["provider"],
        "email_address": row["email_address"],
        "message": "credenciales presentes" if ok else "faltan credenciales (autoriza OAuth o completa IMAP)",
    }


# ──────────────────────────────────────────────────────────────────────
# OAuth · stubs útiles (devuelven URL de autorización si las credenciales
# están en env, o explican qué setear en caso contrario)
# ──────────────────────────────────────────────────────────────────────


def _public_base() -> str:
    return os.getenv("PUBLIC_BACKEND_URL", "https://api.lexai.local")


@router.get("/oauth/{provider}/start")
async def oauth_start(
    provider: str,
    integration_id: str = Query(..., description="email_integrations.id ya creado"),
    principal: Principal = Depends(get_current_firm),
):
    if provider not in ("gmail", "outlook"):
        raise HTTPException(400, "provider no soportado")
    if provider == "gmail":
        client_id = os.getenv("GMAIL_OAUTH_CLIENT_ID")
        if not client_id:
            return {
                "configured": False,
                "instructions": "Configura GMAIL_OAUTH_CLIENT_ID y GMAIL_OAUTH_CLIENT_SECRET en Railway. Ver Google Cloud Console → APIs → OAuth Client.",
            }
        params = {
            "client_id": client_id,
            "redirect_uri": f"{_public_base()}/v1/email/oauth/gmail/callback",
            "response_type": "code",
            "scope": " ".join(GMAIL_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": integration_id,
        }
        return {"configured": True, "auth_url": f"{GMAIL_AUTH_URL}?{urlencode(params)}"}
    # outlook
    client_id = os.getenv("OUTLOOK_OAUTH_CLIENT_ID")
    if not client_id:
        return {
            "configured": False,
            "instructions": "Configura OUTLOOK_OAUTH_CLIENT_ID y OUTLOOK_OAUTH_CLIENT_SECRET en Railway. Ver Azure AD → App registrations.",
        }
    params = {
        "client_id": client_id,
        "redirect_uri": f"{_public_base()}/v1/email/oauth/outlook/callback",
        "response_type": "code",
        "scope": " ".join(OUTLOOK_SCOPES),
        "state": integration_id,
    }
    return {"configured": True, "auth_url": f"{OUTLOOK_AUTH_URL}?{urlencode(params)}"}


@router.get("/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    code: str = Query(...),
    state: str = Query(...),
):
    """Stub: marca la integración como connected. Real impl debe canjear `code`
    contra el endpoint de tokens y guardar access_token + refresh_token.

    Para no bloquear el flujo manual, este callback hace lo posible:
    si tenemos secrets, intenta el token exchange; si no, marca status=connected
    para que el usuario pueda probar el resto del flujo.
    """
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")

    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    if provider == "gmail":
        client_id = os.getenv("GMAIL_OAUTH_CLIENT_ID")
        client_secret = os.getenv("GMAIL_OAUTH_CLIENT_SECRET")
        if client_id and client_secret:
            access_token, refresh_token = await _exchange_gmail(code, client_id, client_secret)
    elif provider == "outlook":
        client_id = os.getenv("OUTLOOK_OAUTH_CLIENT_ID")
        client_secret = os.getenv("OUTLOOK_OAUTH_CLIENT_SECRET")
        if client_id and client_secret:
            access_token, refresh_token = await _exchange_outlook(code, client_id, client_secret)
    else:
        raise HTTPException(400, "provider no soportado")

    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            update email_integrations set
              oauth_access_token = coalesce($2, oauth_access_token),
              oauth_refresh_token = coalesce($3, oauth_refresh_token),
              status = 'connected', updated_at = now()
             where id = $1::uuid
            """,
            state, access_token, refresh_token,
        )
    return {"ok": True, "integration_id": state, "provider": provider, "exchanged": bool(access_token)}


async def _exchange_gmail(code: str, client_id: str, client_secret: str):
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(GMAIL_TOKEN_URL, data={
                "code": code, "client_id": client_id, "client_secret": client_secret,
                "redirect_uri": f"{_public_base()}/v1/email/oauth/gmail/callback",
                "grant_type": "authorization_code",
            })
            if r.status_code == 200:
                d = r.json()
                return d.get("access_token"), d.get("refresh_token")
    except Exception as e:
        logger.warning("gmail exchange failed: %s", e)
    return None, None


async def _exchange_outlook(code: str, client_id: str, client_secret: str):
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(OUTLOOK_TOKEN_URL, data={
                "code": code, "client_id": client_id, "client_secret": client_secret,
                "redirect_uri": f"{_public_base()}/v1/email/oauth/outlook/callback",
                "grant_type": "authorization_code", "scope": " ".join(OUTLOOK_SCOPES),
            })
            if r.status_code == 200:
                d = r.json()
                return d.get("access_token"), d.get("refresh_token")
    except Exception as e:
        logger.warning("outlook exchange failed: %s", e)
    return None, None
