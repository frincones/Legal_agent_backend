"""Sprint 7 · Calendar sync worker.

Sincroniza eventos desde Google Calendar / Outlook Calendar al cache local
`calendar_events`. Si auto_create_deadlines=true y el evento contiene
`#lexai` (case-insensitive) en title/description, crea/actualiza un
matter_deadline correspondiente.

Detección de matter_id:
  1. Parse tokens tipo `#caso:<expediente>` o `#matter:<uuid>` en title/desc
  2. Match por expediente directo en title
  3. Si no, deadline queda sin matter_id (visible en feed general)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

GCAL_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{cal}/events"
GRAPH_EVENTS_URL = "https://graph.microsoft.com/v1.0/me/calendar/events"


async def sync_calendar_integration(integration_id: str) -> dict:
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage unavailable"}
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, firm_id, user_id, provider, email_address, active,
                   oauth_access_token, oauth_refresh_token,
                   oauth_access_token_enc, oauth_refresh_token_enc,
                   primary_calendar_id, auto_create_deadlines,
                   sync_token, delta_link
              from calendar_integrations
             where id = $1::uuid
            """,
            integration_id,
        )
    if not row:
        return {"error": "not found"}
    if not row["active"]:
        return {"skipped": "inactive"}
    try:
        if row["provider"] == "google":
            result = await _sync_google(row)
        elif row["provider"] == "outlook":
            result = await _sync_outlook(row)
        else:
            return {"error": "provider"}
    except Exception as e:
        logger.exception("calendar sync failed")
        await _mark(integration_id, "error", str(e)[:500])
        return {"error": str(e)}
    await _mark(integration_id, "ok", None)
    return result


async def sync_all_for_firm(firm_id: str) -> dict:
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage unavailable"}
    async with storage.pool.acquire() as conn:
        ids = await conn.fetch(
            "select id from calendar_integrations where firm_id = $1::uuid and active = true",
            firm_id,
        )
    total_inserted = 0
    synced = 0
    errors: list[str] = []
    for r in ids:
        result = await sync_calendar_integration(str(r["id"]))
        synced += 1
        total_inserted += result.get("inserted", 0) or 0
        if "error" in result:
            errors.append(result["error"])
    return {"synced": synced, "inserted": total_inserted, "errors": errors}


async def sync_all_active() -> dict:
    """Sprint B · sync delta para TODAS las integraciones activas del sistema.

    Invocado por pg_cron cada 15min via /v1/admin/sync-tick?type=calendar.
    Idempotente · si una integración falla, las otras siguen.
    """
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage unavailable"}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id from calendar_integrations
             where active = true and status = 'connected'
             order by coalesce(last_synced_at, '1970-01-01'::timestamptz) asc
             limit 200
            """,
        )
    total_inserted = 0
    synced = 0
    errors: list[str] = []
    for r in rows:
        try:
            result = await sync_calendar_integration(str(r["id"]))
            synced += 1
            total_inserted += result.get("inserted", 0) or 0
            if "error" in result:
                errors.append(f"{r['id']}: {result['error']}")
        except Exception as e:
            logger.warning("sync_all_active item %s failed: %s", r["id"], e)
            errors.append(f"{r['id']}: {str(e)[:120]}")
    return {"synced": synced, "inserted": total_inserted,
            "errors": errors[:10], "error_count": len(errors)}


async def _mark(integration_id, status, error):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """update calendar_integrations set last_synced_at=now(),
                  last_status=$2, last_error=$3 where id=$1::uuid""",
            integration_id, status, error,
        )


def _token(row) -> Optional[str]:
    from utils.crypto import decrypt
    if row["oauth_access_token_enc"]:
        plain = decrypt(row["oauth_access_token_enc"])
        if plain:
            return plain
    return row["oauth_access_token"]


async def _sync_google(row) -> dict:
    access = _token(row)
    if not access:
        return {"inserted": 0, "skipped": "no_token"}
    cal_id = row["primary_calendar_id"] or "primary"
    try:
        import httpx
        async with httpx.AsyncClient(timeout=20) as client:
            url = GCAL_EVENTS_URL.format(cal=cal_id)
            params = {"maxResults": 100, "singleEvents": "true",
                      "orderBy": "startTime",
                      "timeMin": datetime.now(timezone.utc).isoformat()}
            r = await client.get(url, headers={"Authorization": f"Bearer {access}"}, params=params)
            if r.status_code != 200:
                return {"inserted": 0, "error": f"google HTTP {r.status_code}"}
            data = r.json() or {}
            events = data.get("items") or []
    except Exception as e:
        return {"inserted": 0, "error": str(e)}

    return await _persist_events(row, events, _gcal_normalize)


async def _sync_outlook(row) -> dict:
    access = _token(row)
    if not access:
        return {"inserted": 0, "skipped": "no_token"}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                GRAPH_EVENTS_URL,
                headers={"Authorization": f"Bearer {access}"},
                params={"$top": "100", "$orderby": "start/dateTime",
                        "$filter": f"start/dateTime ge '{datetime.now(timezone.utc).isoformat()}'"},
            )
            if r.status_code != 200:
                return {"inserted": 0, "error": f"outlook HTTP {r.status_code}"}
            events = (r.json() or {}).get("value") or []
    except Exception as e:
        return {"inserted": 0, "error": str(e)}
    return await _persist_events(row, events, _outlook_normalize)


def _gcal_normalize(e: dict) -> dict:
    start = (e.get("start") or {})
    end = (e.get("end") or {})
    start_at = _parse_iso(start.get("dateTime") or start.get("date"))
    end_at = _parse_iso(end.get("dateTime") or end.get("date"))
    return {
        "external_id": e.get("id"),
        "title": e.get("summary") or "",
        "description": e.get("description") or "",
        "location": e.get("location"),
        "start_at": start_at,
        "end_at": end_at,
        "is_all_day": "date" in start and "dateTime" not in start,
        "meeting_url": (e.get("hangoutLink") or
                        ((e.get("conferenceData") or {}).get("entryPoints") or [{}])[0].get("uri") if e.get("conferenceData") else None),
        "status": e.get("status", "confirmed"),
        "raw": e,
    }


def _outlook_normalize(e: dict) -> dict:
    start = (e.get("start") or {})
    end = (e.get("end") or {})
    return {
        "external_id": e.get("id"),
        "title": e.get("subject") or "",
        "description": (e.get("bodyPreview") or ""),
        "location": ((e.get("location") or {}).get("displayName")),
        "start_at": _parse_iso(start.get("dateTime")),
        "end_at": _parse_iso(end.get("dateTime")),
        "is_all_day": bool(e.get("isAllDay")),
        "meeting_url": ((e.get("onlineMeeting") or {}).get("joinUrl")),
        "status": "confirmed" if not e.get("isCancelled") else "canceled",
        "raw": e,
    }


def _parse_iso(s: Optional[str]):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


_TAG_RE = re.compile(r"#lexai\b", re.IGNORECASE)
_CASO_RE = re.compile(r"#(?:caso|matter):([A-Za-z0-9\-]+)", re.IGNORECASE)


async def _persist_events(row, events, normalize) -> dict:
    from utils.db import get_storage
    storage = await get_storage()
    inserted = 0
    for raw in events:
        parsed = normalize(raw)
        if not parsed.get("external_id") or not parsed.get("start_at"):
            continue
        title = parsed.get("title") or ""
        desc = parsed.get("description") or ""
        text_blob = f"{title}\n{desc}"
        has_tag = bool(_TAG_RE.search(text_blob))
        matter_id = await _resolve_matter(row["firm_id"], text_blob)
        source_tag = "#lexai" if has_tag else None

        # Upsert calendar_events
        async with storage.pool.acquire() as conn:
            ev = await conn.fetchrow(
                """
                insert into calendar_events
                  (firm_id, integration_id, external_id, calendar_id, title, description,
                   location, start_at, end_at, is_all_day, meeting_url, status, source_tag,
                   matter_id, raw)
                values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8::timestamptz,
                        $9::timestamptz, $10, $11, $12, $13, $14::uuid, $15::jsonb)
                on conflict (integration_id, external_id) do update set
                  title = excluded.title,
                  description = excluded.description,
                  location = excluded.location,
                  start_at = excluded.start_at,
                  end_at = excluded.end_at,
                  status = excluded.status,
                  source_tag = excluded.source_tag,
                  matter_id = coalesce(calendar_events.matter_id, excluded.matter_id),
                  updated_at = now()
                returning id, deadline_id, matter_id
                """,
                row["firm_id"], row["id"],
                parsed["external_id"], row["primary_calendar_id"],
                title, desc, parsed.get("location"),
                parsed["start_at"], parsed.get("end_at"),
                parsed.get("is_all_day", False), parsed.get("meeting_url"),
                parsed.get("status", "confirmed"), source_tag,
                str(matter_id) if matter_id else None,
                __import__("json").dumps(raw),
            )
        if not ev:
            continue
        inserted += 1
        # Auto-crear matter_deadline si está marcado y la integración lo permite
        if has_tag and matter_id and row["auto_create_deadlines"] and not ev["deadline_id"]:
            try:
                async with storage.pool.acquire() as conn:
                    dl = await conn.fetchrow(
                        """
                        insert into matter_deadlines (firm_id, matter_id, titulo, fecha, tipo, origen, completado)
                        values ($1::uuid, $2::uuid, $3, $4::date, 'audiencia', 'calendario', false)
                        returning id
                        """,
                        row["firm_id"], matter_id,
                        title[:200], parsed["start_at"].date() if parsed["start_at"] else None,
                    )
                    if dl:
                        await conn.execute(
                            "update calendar_events set deadline_id = $2::uuid where id = $1::uuid",
                            ev["id"], dl["id"],
                        )
            except Exception as e:
                logger.debug("auto-create deadline skipped: %s", e)
    return {"inserted": inserted, "total": len(events)}


async def _resolve_matter(firm_id, text: str) -> Optional[str]:
    """Intenta encontrar el matter referenciado en title/desc del evento."""
    m = _CASO_RE.search(text or "")
    candidate = m.group(1) if m else None
    if not candidate:
        return None
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        # 1. UUID directo
        try:
            row = await conn.fetchrow(
                "select id from matters where firm_id = $1::uuid and id = $2::uuid",
                firm_id, candidate,
            )
            if row:
                return str(row["id"])
        except Exception:
            pass
        # 2. Expediente match
        row = await conn.fetchrow(
            "select id from matters where firm_id = $1::uuid and expediente = $2 limit 1",
            firm_id, candidate,
        )
        if row:
            return str(row["id"])
    return None


# ════════════════════════════════════════════════════════════════════════
# Voice tool
# ════════════════════════════════════════════════════════════════════════


async def sync_calendar_tool(args: dict, ctx: dict) -> dict:
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    return await sync_all_for_firm(firm_id)
