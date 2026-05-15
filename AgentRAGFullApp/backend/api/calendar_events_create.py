"""Sprint B · POST /v1/calendar/events · crea audiencia desde LexAI.

Distinto del endpoint legacy /v1/calendar/events (GET) que lista eventos.
Este crea evento en el provider conectado del usuario + persiste en
calendar_events. Si auto_push_lexai_events=true (default) hace push real.

Flujo:
  1. Buscar integration activa del usuario (preferencia: provider preferido)
  2. Descifrar access_token
  3. Si está expirado · refrescar via refresh_access_token()
  4. Llamar Google/Outlook crear evento (con Meet/Teams si corresponde)
  5. Insert en calendar_events con pushed_by_lexai=true
  6. Retornar evento creado al frontend
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm
from utils import crypto
from utils.oauth import refresh_access_token
from utils.calendar_client import (
    google_create_event,
    outlook_create_event,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/calendar", tags=["calendar"])


class CreateAudienciaBody(BaseModel):
    matter_id: Optional[UUID] = None
    kind: str = Field(default="audiencia", pattern="^(audiencia|conciliacion|reunion|plazo|otro)$")
    title: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    location: Optional[str] = None
    start_at: datetime
    end_at: datetime
    attendees: list[str] = Field(default_factory=list)
    create_conference: bool = False  # Meet/Teams auto según provider
    timezone: str = "America/Bogota"


def _validate_dates(start: datetime, end: datetime):
    if end <= start:
        raise HTTPException(400, "end_at debe ser posterior a start_at")
    if start < datetime.now(timezone.utc) - timedelta(hours=1):
        raise HTTPException(400, "start_at no puede estar más de 1h en el pasado")


async def _pick_active_integration(pool, firm_id: str, user_id: str):
    """Retorna integración activa preferida. Prioriza:
    1) auto_push_lexai_events=true, 2) provider=google, 3) más reciente."""
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            select id, provider, email_address, primary_calendar_id,
                   oauth_access_token_enc, oauth_refresh_token_enc, oauth_expires_at,
                   preferred_conference, auto_push_lexai_events
              from calendar_integrations
             where firm_id = $1::uuid
               and user_id = $2::uuid
               and active = true
               and status = 'connected'
               and auto_push_lexai_events = true
             order by case when provider='google' then 1 when provider='outlook' then 2 else 3 end,
                      updated_at desc
             limit 1
            """,
            firm_id, user_id,
        )


async def _get_valid_access_token(pool, integration) -> Optional[str]:
    """Descifra access_token. Si está expirado o por expirar (<5min), refresca."""
    access = crypto.decrypt(integration["oauth_access_token_enc"])
    if not access:
        return None
    expires_at = integration.get("oauth_expires_at")
    now = datetime.now(timezone.utc)
    if expires_at and expires_at < now + timedelta(minutes=5):
        # Refrescar
        refresh = crypto.decrypt(integration["oauth_refresh_token_enc"])
        if not refresh:
            logger.warning("No refresh token available; cannot refresh expired access")
            return access  # try with possibly-stale token
        new_tokens = await refresh_access_token(integration["provider"], refresh_token=refresh)
        if new_tokens and new_tokens.get("access_token"):
            new_access = new_tokens["access_token"]
            # Persistir nuevo token
            new_access_enc = crypto.encrypt(new_access)
            new_expires_at = now + timedelta(seconds=int(new_tokens.get("expires_in") or 3600))
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    update calendar_integrations
                       set oauth_access_token_enc = $1,
                           oauth_expires_at = $2,
                           updated_at = now()
                     where id = $3
                    """,
                    new_access_enc, new_expires_at, integration["id"],
                )
            return new_access
    return access


@router.post("/events")
async def create_audiencia(
    body: CreateAudienciaBody,
    principal: Principal = Depends(get_current_firm),
):
    """Crea una audiencia · push a Calendar provider + persiste en calendar_events."""
    _validate_dates(body.start_at, body.end_at)

    from utils.db import get_storage
    storage = await get_storage()

    integration = await _pick_active_integration(
        storage.pool, principal.firm_id, principal.user_id
    )
    if not integration:
        raise HTTPException(
            409,
            detail={
                "error": "no_calendar_connected",
                "message": "Conecta un calendario antes de crear audiencias. "
                           "Ve a /settings/integraciones.",
            },
        )

    access_token = await _get_valid_access_token(storage.pool, integration)
    if not access_token:
        raise HTTPException(500, "Failed to obtain access token")

    provider = integration["provider"]
    pref_conf = integration.get("preferred_conference")
    create_meet = body.create_conference and pref_conf == "meet"
    create_teams = body.create_conference and pref_conf == "teams"

    if provider == "google":
        ext = await google_create_event(
            access_token=access_token,
            calendar_id=integration.get("primary_calendar_id") or "primary",
            summary=body.title,
            description=body.description,
            location=body.location,
            start_at=body.start_at,
            end_at=body.end_at,
            attendees_emails=body.attendees,
            create_meet_link=create_meet,
            timezone_str=body.timezone,
        )
        if not ext:
            raise HTTPException(502, "Google Calendar event creation failed")
        external_id = ext.get("id")
        meeting_url = None
        if ext.get("hangoutLink"):
            meeting_url = ext["hangoutLink"]
        elif ext.get("conferenceData", {}).get("entryPoints"):
            for ep in ext["conferenceData"]["entryPoints"]:
                if ep.get("entryPointType") == "video":
                    meeting_url = ep.get("uri")
                    break
        conf_provider = "meet" if meeting_url else None

    elif provider == "outlook":
        ext = await outlook_create_event(
            access_token=access_token,
            subject=body.title,
            body_text=body.description,
            location=body.location,
            start_at=body.start_at,
            end_at=body.end_at,
            attendees_emails=body.attendees,
            create_teams_link=create_teams,
            timezone_str=body.timezone,
        )
        if not ext:
            raise HTTPException(502, "Outlook event creation failed")
        external_id = ext.get("id")
        om = ext.get("onlineMeeting") or {}
        meeting_url = om.get("joinUrl")
        conf_provider = "teams" if meeting_url else None

    else:
        raise HTTPException(400, f"Unsupported provider: {provider}")

    # Persistir en calendar_events
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into calendar_events
              (firm_id, integration_id, matter_id, external_id,
               calendar_id, title, description, location,
               start_at, end_at, attendees, meeting_url,
               status, source_tag, raw,
               pushed_by_lexai, lexai_event_kind, conference_provider,
               last_synced_at)
            values ($1::uuid, $2::uuid, $3,
                    $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12,
                    'confirmed', 'lexai', $13::jsonb,
                    true, $14, $15, now())
            returning id
            """,
            principal.firm_id, integration["id"], body.matter_id,
            external_id,
            integration.get("primary_calendar_id") or "primary",
            body.title, body.description, body.location,
            body.start_at, body.end_at,
            json.dumps([{"email": e} for e in body.attendees]),
            meeting_url,
            json.dumps(ext)[:30000],
            body.kind, conf_provider,
        )

    return {
        "ok": True,
        "id": str(row["id"]),
        "external_id": external_id,
        "provider": provider,
        "meeting_url": meeting_url,
        "title": body.title,
        "start_at": body.start_at.isoformat(),
        "end_at": body.end_at.isoformat(),
    }
