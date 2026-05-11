"""Sprint 7 · Calendar integrations API (Google Calendar + Outlook).

Conecta calendarios para que LexAI:
  · sincronice audiencias y eventos con #lexai como matter_deadlines
  · muestre los próximos eventos en el inicio del abogado
  · cree eventos cuando el agente programa una audiencia

Endpoints:
  GET    /v1/calendar/integrations
  POST   /v1/calendar/integrations           (provider, email)
  PATCH  /v1/calendar/integrations/{id}
  DELETE /v1/calendar/integrations/{id}
  POST   /v1/calendar/integrations/{id}/sync-now
  GET    /v1/calendar/oauth/{provider}/start
  GET    /v1/calendar/oauth/{provider}/callback
  GET    /v1/calendar/events                 (próximos N eventos del firm)

OAuth real requiere las mismas credenciales que email (Gmail/Outlook OAuth
client IDs) más scopes adicionales de Calendar. Sin credenciales, los
endpoints OAuth devuelven mensaje útil pero no rompen.
"""

from __future__ import annotations

import logging
import os
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/calendar", tags=["calendar"])


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CAL_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]
OUTLOOK_AUTH_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
OUTLOOK_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
OUTLOOK_CAL_SCOPES = ["offline_access", "Calendars.ReadWrite"]


def _public_base() -> str:
    return os.getenv("PUBLIC_BACKEND_URL", "https://api.lexai.local")


class CreateRequest(BaseModel):
    provider: str = Field(pattern="^(google|outlook)$")
    email_address: str
    display_name: Optional[str] = None
    primary_calendar_id: Optional[str] = None
    auto_create_deadlines: bool = True


class PatchRequest(BaseModel):
    active: Optional[bool] = None
    display_name: Optional[str] = None
    primary_calendar_id: Optional[str] = None
    auto_create_deadlines: Optional[bool] = None


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
                   primary_calendar_id, auto_create_deadlines, created_at
              from calendar_integrations
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
                "primary_calendar_id": r["primary_calendar_id"],
                "auto_create_deadlines": r["auto_create_deadlines"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


@router.post("/integrations")
async def create_integration(
    body: CreateRequest,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into calendar_integrations
              (firm_id, user_id, provider, email_address, display_name,
               primary_calendar_id, auto_create_deadlines, status)
            values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, 'pending')
            on conflict (user_id, provider, email_address) do update set
              display_name = excluded.display_name,
              primary_calendar_id = excluded.primary_calendar_id,
              auto_create_deadlines = excluded.auto_create_deadlines,
              active = true, updated_at = now()
            returning id, provider, status
            """,
            principal.firm_id, principal.user_id, body.provider,
            body.email_address.lower(), body.display_name,
            body.primary_calendar_id, body.auto_create_deadlines,
        )
    return {
        "id": str(row["id"]),
        "provider": row["provider"],
        "status": row["status"],
        "next_step": "Visita /v1/calendar/oauth/{provider}/start para autorizar",
    }


@router.patch("/integrations/{integration_id}")
async def patch_integration(
    integration_id: str,
    body: PatchRequest,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    fields, params = [], [integration_id, principal.firm_id]
    for f in ("active", "display_name", "primary_calendar_id", "auto_create_deadlines"):
        v = getattr(body, f)
        if v is not None:
            params.append(v); fields.append(f"{f} = ${len(params)}")
    if not fields:
        raise HTTPException(400, "no fields to update")
    sql = f"""update calendar_integrations set {', '.join(fields)}, updated_at = now()
              where id = $1::uuid and firm_id = $2::uuid returning id"""
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(sql, *params)
    if not row:
        raise HTTPException(404, "not found")
    return {"id": str(row["id"]), "ok": True}


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
        await conn.execute(
            "delete from calendar_integrations where id = $1::uuid and firm_id = $2::uuid",
            integration_id, principal.firm_id,
        )
    return {"deleted": True}


@router.post("/integrations/{integration_id}/sync-now")
async def sync_now(
    integration_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        ok = await conn.fetchval(
            "select 1 from calendar_integrations where id = $1::uuid and firm_id = $2::uuid",
            integration_id, principal.firm_id,
        )
    if not ok:
        raise HTTPException(404, "not found")
    from agent.workers.calendar_sync import sync_calendar_integration
    return await sync_calendar_integration(integration_id)


@router.post("/sync-all")
async def sync_all(principal: Principal = Depends(get_current_firm)):
    from agent.workers.calendar_sync import sync_all_for_firm
    return await sync_all_for_firm(principal.firm_id)


# ──────────────────────────────────────────────────────────────────────
# OAuth start + callback
# ──────────────────────────────────────────────────────────────────────


@router.get("/oauth/{provider}/start")
async def oauth_start(
    provider: str,
    integration_id: str = Query(...),
    principal: Principal = Depends(get_current_firm),
):
    if provider == "google":
        cid = os.getenv("GMAIL_OAUTH_CLIENT_ID") or os.getenv("GOOGLE_OAUTH_CLIENT_ID")
        if not cid:
            return {"configured": False, "instructions": "Setea GMAIL_OAUTH_CLIENT_ID + SECRET (Calendar scope incluido)"}
        params = {
            "client_id": cid,
            "redirect_uri": f"{_public_base()}/v1/calendar/oauth/google/callback",
            "response_type": "code",
            "scope": " ".join(GOOGLE_CAL_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": integration_id,
        }
        return {"configured": True, "auth_url": f"{GOOGLE_AUTH_URL}?{urlencode(params)}"}
    if provider == "outlook":
        cid = os.getenv("OUTLOOK_OAUTH_CLIENT_ID")
        if not cid:
            return {"configured": False, "instructions": "Setea OUTLOOK_OAUTH_CLIENT_ID + SECRET"}
        params = {
            "client_id": cid,
            "redirect_uri": f"{_public_base()}/v1/calendar/oauth/outlook/callback",
            "response_type": "code",
            "scope": " ".join(OUTLOOK_CAL_SCOPES),
            "state": integration_id,
        }
        return {"configured": True, "auth_url": f"{OUTLOOK_AUTH_URL}?{urlencode(params)}"}
    raise HTTPException(400, "provider no soportado")


@router.get("/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    code: str = Query(...),
    state: str = Query(...),
):
    from utils.db import get_storage
    from utils.crypto import encrypt_or_passthrough
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")

    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    if provider == "google":
        cid = os.getenv("GMAIL_OAUTH_CLIENT_ID") or os.getenv("GOOGLE_OAUTH_CLIENT_ID")
        cs = os.getenv("GMAIL_OAUTH_CLIENT_SECRET") or os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
        if cid and cs:
            access_token, refresh_token = await _exchange(GOOGLE_TOKEN_URL, code, cid, cs, f"{_public_base()}/v1/calendar/oauth/google/callback")
    elif provider == "outlook":
        cid = os.getenv("OUTLOOK_OAUTH_CLIENT_ID")
        cs = os.getenv("OUTLOOK_OAUTH_CLIENT_SECRET")
        if cid and cs:
            access_token, refresh_token = await _exchange(
                OUTLOOK_TOKEN_URL, code, cid, cs,
                f"{_public_base()}/v1/calendar/oauth/outlook/callback",
                extra={"scope": " ".join(OUTLOOK_CAL_SCOPES)},
            )
    else:
        raise HTTPException(400, "provider no soportado")

    enc_a, ver_a = encrypt_or_passthrough(access_token)
    enc_r, ver_r = encrypt_or_passthrough(refresh_token)
    legacy_a = access_token if ver_a == 0 else None
    legacy_r = refresh_token if ver_r == 0 else None
    ver = max(ver_a, ver_r) if (enc_a or enc_r) else 0

    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            update calendar_integrations set
              oauth_access_token = coalesce($2, oauth_access_token),
              oauth_refresh_token = coalesce($3, oauth_refresh_token),
              oauth_access_token_enc = coalesce($4, oauth_access_token_enc),
              oauth_refresh_token_enc = coalesce($5, oauth_refresh_token_enc),
              encryption_version = greatest(coalesce(encryption_version,0), $6),
              status = 'connected', updated_at = now()
             where id = $1::uuid
            """,
            state, legacy_a, legacy_r, enc_a, enc_r, ver,
        )
    return {"ok": True, "integration_id": state, "provider": provider, "exchanged": bool(access_token)}


async def _exchange(token_url, code, cid, cs, redirect, extra=None):
    try:
        import httpx
        data = {
            "code": code, "client_id": cid, "client_secret": cs,
            "redirect_uri": redirect, "grant_type": "authorization_code",
        }
        if extra:
            data.update(extra)
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(token_url, data=data)
            if r.status_code == 200:
                d = r.json()
                return d.get("access_token"), d.get("refresh_token")
    except Exception as e:
        logger.warning("token exchange failed: %s", e)
    return None, None


# ──────────────────────────────────────────────────────────────────────
# Eventos cacheados
# ──────────────────────────────────────────────────────────────────────


@router.get("/events")
async def list_events(
    days: int = Query(default=14, le=60),
    matter_id: Optional[str] = None,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    where = ["firm_id = $1::uuid", "start_at between now() and now() + ($2::int || ' days')::interval"]
    params: list = [principal.firm_id, days]
    if matter_id:
        params.append(matter_id); where.append(f"matter_id = ${len(params)}::uuid")
    sql = f"""
        select id, matter_id, title, description, location, start_at, end_at,
               is_all_day, meeting_url, status, source_tag
          from calendar_events
         where {' and '.join(where)}
         order by start_at asc
         limit 200
    """
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]),
                "matter_id": str(r["matter_id"]) if r["matter_id"] else None,
                "title": r["title"],
                "description": r["description"],
                "location": r["location"],
                "start_at": r["start_at"].isoformat() if r["start_at"] else None,
                "end_at": r["end_at"].isoformat() if r["end_at"] else None,
                "is_all_day": r["is_all_day"],
                "meeting_url": r["meeting_url"],
                "status": r["status"],
                "source_tag": r["source_tag"],
            }
            for r in rows
        ],
    }
