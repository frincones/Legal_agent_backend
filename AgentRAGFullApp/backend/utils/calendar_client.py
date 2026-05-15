"""Sprint B · Cliente unificado de Google Calendar + Microsoft Graph Calendar.

Provee:
  - create_event()    · crear evento en el provider con link Meet/Teams opcional
  - update_event()    · actualizar
  - delete_event()    · borrar
  - list_changes()    · delta sync · syncToken (Google) / deltaLink (Graph)

Patrón:
  - El access_token se descifra en el caller con utils.crypto
  - Si el token expiró, el caller debe refrescar primero con utils.oauth
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

GOOGLE_CAL_BASE = "https://www.googleapis.com/calendar/v3"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


# ─────────────────────────────────────────────────────────────────────
# Google Calendar
# ─────────────────────────────────────────────────────────────────────

async def google_create_event(
    *,
    access_token: str,
    calendar_id: str = "primary",
    summary: str,
    description: Optional[str] = None,
    location: Optional[str] = None,
    start_at: datetime,
    end_at: datetime,
    attendees_emails: Optional[list[str]] = None,
    create_meet_link: bool = False,
    timezone_str: str = "America/Bogota",
) -> Optional[dict[str, Any]]:
    """Crea un evento en Google Calendar. Retorna el dict del evento creado."""
    payload: dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": start_at.isoformat(), "timeZone": timezone_str},
        "end": {"dateTime": end_at.isoformat(), "timeZone": timezone_str},
    }
    if description:
        payload["description"] = description
    if location:
        payload["location"] = location
    if attendees_emails:
        payload["attendees"] = [{"email": e} for e in attendees_emails]
    if create_meet_link:
        # Pedir a Google que cree un Meet vía conferenceDataVersion=1
        payload["conferenceData"] = {
            "createRequest": {
                "requestId": f"lexai-{int(start_at.timestamp())}",
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }

    url = f"{GOOGLE_CAL_BASE}/calendars/{calendar_id}/events"
    params = {"conferenceDataVersion": "1" if create_meet_link else "0",
              "sendUpdates": "all"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                url,
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
                json=payload,
            )
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.warning("google_create_event failed: %s", e)
        return None


async def google_delete_event(
    *,
    access_token: str,
    calendar_id: str,
    event_id: str,
) -> bool:
    url = f"{GOOGLE_CAL_BASE}/calendars/{calendar_id}/events/{event_id}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.delete(
                url, headers={"Authorization": f"Bearer {access_token}"}
            )
            return r.status_code in (200, 204)
    except Exception as e:
        logger.warning("google_delete_event failed: %s", e)
        return False


async def google_list_changes(
    *,
    access_token: str,
    calendar_id: str = "primary",
    sync_token: Optional[str] = None,
    time_min: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """Lista cambios desde el último sync_token. Si no hay token,
    arranca un sync completo. Retorna dict con keys 'items' y 'nextSyncToken'."""
    url = f"{GOOGLE_CAL_BASE}/calendars/{calendar_id}/events"
    params: dict[str, str] = {
        "maxResults": "250",
        "singleEvents": "true",
    }
    if sync_token:
        params["syncToken"] = sync_token
    else:
        # Primera sync: solo eventos futuros (no backfill todo el calendario)
        if time_min:
            params["timeMin"] = time_min.isoformat()
        else:
            params["timeMin"] = datetime.now(timezone.utc).isoformat()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if r.status_code == 410:
                # syncToken expired · caller debe full re-sync
                return {"items": [], "nextSyncToken": None, "expired": True}
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.warning("google_list_changes failed: %s", e)
        return None


# ─────────────────────────────────────────────────────────────────────
# Microsoft Graph Calendar (Outlook)
# ─────────────────────────────────────────────────────────────────────

async def outlook_create_event(
    *,
    access_token: str,
    subject: str,
    body_text: Optional[str] = None,
    location: Optional[str] = None,
    start_at: datetime,
    end_at: datetime,
    attendees_emails: Optional[list[str]] = None,
    create_teams_link: bool = False,
    timezone_str: str = "America/Bogota",
) -> Optional[dict[str, Any]]:
    """Crea evento en Outlook Calendar vía Microsoft Graph."""
    payload: dict[str, Any] = {
        "subject": subject,
        "start": {"dateTime": start_at.isoformat(), "timeZone": timezone_str},
        "end": {"dateTime": end_at.isoformat(), "timeZone": timezone_str},
    }
    if body_text:
        payload["body"] = {"contentType": "HTML", "content": body_text}
    if location:
        payload["location"] = {"displayName": location}
    if attendees_emails:
        payload["attendees"] = [
            {"emailAddress": {"address": e}, "type": "required"}
            for e in attendees_emails
        ]
    if create_teams_link:
        payload["isOnlineMeeting"] = True
        payload["onlineMeetingProvider"] = "teamsForBusiness"

    url = f"{GRAPH_BASE}/me/events"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.warning("outlook_create_event failed: %s", e)
        return None


async def outlook_delete_event(
    *,
    access_token: str,
    event_id: str,
) -> bool:
    url = f"{GRAPH_BASE}/me/events/{event_id}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.delete(
                url, headers={"Authorization": f"Bearer {access_token}"}
            )
            return r.status_code in (200, 204)
    except Exception as e:
        logger.warning("outlook_delete_event failed: %s", e)
        return False


async def outlook_list_changes(
    *,
    access_token: str,
    delta_link: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Delta query en Graph Calendar. Si no hay delta_link, usa /calendarView/delta
    con ventana de 60 días."""
    if delta_link:
        url = delta_link
        params = None
    else:
        url = f"{GRAPH_BASE}/me/calendarView/delta"
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        start = now.isoformat()
        end = (now + timedelta(days=60)).isoformat()
        params = {"startDateTime": start, "endDateTime": end}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Prefer": 'odata.maxpagesize=250',
                },
            )
            r.raise_for_status()
            data = r.json()
            # Graph paginate via @odata.deltaLink (final) o @odata.nextLink (intermedio)
            return {
                "items": data.get("value", []),
                "nextDeltaLink": data.get("@odata.deltaLink"),
                "nextLink": data.get("@odata.nextLink"),
            }
    except Exception as e:
        logger.warning("outlook_list_changes failed: %s", e)
        return None
