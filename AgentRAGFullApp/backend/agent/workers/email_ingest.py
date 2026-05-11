"""Sprint 6 · Email ingest worker.

Hace polling real de las cuentas conectadas en email_integrations:
  - Gmail · API REST (users.messages.list + messages.get)
  - Outlook (Microsoft Graph) · /me/messages
  - IMAP · imaplib (síncrono, ejecutado en thread)

Para cada mensaje nuevo (no existente en email_messages.external_id):
  1. Inserta el row con subject/from/body/snippet
  2. Llama a parse_legal_email_tool para clasificar legalmente
  3. (Opcional) Lanza push si severidad >= 'alta'

Tolerante a fallos: si una cuenta falla, las demás siguen.
Idempotente: usa external_id como dedup key.

Punto de entrada:
  POST /v1/email/integrations/{id}/sync-now    (admin / dueño)
  POST /v1/email/sync-all                       (admin)
  Voice tool: 'LexAI, revisa mi correo'
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

GMAIL_LIST_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
GMAIL_GET_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/{}"
GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"


async def sync_integration(integration_id: str) -> dict:
    """Sync una integración de correo. Devuelve métricas."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage unavailable"}

    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, firm_id, user_id, provider, email_address, active, status,
                   oauth_access_token, oauth_refresh_token,
                   oauth_access_token_enc, oauth_refresh_token_enc,
                   oauth_expires_at, encryption_version,
                   imap_host, imap_port, imap_username,
                   imap_password_enc, imap_password_enc_v2,
                   watch_label, filter_query, last_synced_at
              from email_integrations
             where id = $1::uuid
            """,
            integration_id,
        )
    if not row:
        return {"error": "integration not found"}
    if not row["active"]:
        return {"skipped": "inactive"}

    try:
        if row["provider"] == "gmail":
            result = await _sync_gmail(row)
        elif row["provider"] == "outlook":
            result = await _sync_outlook(row)
        elif row["provider"] == "imap":
            result = await _sync_imap(row)
        else:
            return {"error": f"unknown provider: {row['provider']}"}
    except Exception as e:
        logger.exception("email sync failed for %s", integration_id)
        await _mark_synced(integration_id, status="error", error=str(e)[:500])
        return {"error": str(e)}

    await _mark_synced(integration_id, status="ok", error=None)
    return result


async def sync_all_for_firm(firm_id: str) -> dict:
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage unavailable"}
    async with storage.pool.acquire() as conn:
        ids = await conn.fetch(
            """
            select id from email_integrations
             where firm_id = $1::uuid and active = true and status in ('connected','pending')
            """,
            firm_id,
        )
    if not ids:
        return {"synced": 0, "inserted": 0}
    total_inserted = 0
    synced = 0
    errors: list[str] = []
    for r in ids:
        result = await sync_integration(str(r["id"]))
        synced += 1
        total_inserted += result.get("inserted", 0) or 0
        if "error" in result:
            errors.append(result["error"])
    return {"synced": synced, "inserted": total_inserted, "errors": errors}


async def _mark_synced(integration_id: str, status: str, error: Optional[str]) -> None:
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            update email_integrations set
              last_synced_at = now(),
              last_status = $2, last_error = $3
             where id = $1::uuid
            """,
            integration_id, status, error,
        )


def _resolve_token(row, token_col_enc: str, token_col_plain: str) -> Optional[str]:
    """Devuelve el token en claro: si está cifrado lo descifra, si no usa el legacy."""
    from utils.crypto import decrypt
    enc = row[token_col_enc]
    if enc:
        plain = decrypt(enc)
        if plain:
            return plain
    return row[token_col_plain]


async def _refresh_gmail_token(refresh_token: str) -> Optional[str]:
    import os
    cid = os.getenv("GMAIL_OAUTH_CLIENT_ID")
    cs = os.getenv("GMAIL_OAUTH_CLIENT_SECRET")
    if not (cid and cs and refresh_token):
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post("https://oauth2.googleapis.com/token", data={
                "client_id": cid, "client_secret": cs,
                "refresh_token": refresh_token, "grant_type": "refresh_token",
            })
            if r.status_code == 200:
                return (r.json() or {}).get("access_token")
    except Exception as e:
        logger.warning("gmail refresh failed: %s", e)
    return None


async def _sync_gmail(row) -> dict:
    access = _resolve_token(row, "oauth_access_token_enc", "oauth_access_token")
    refresh = _resolve_token(row, "oauth_refresh_token_enc", "oauth_refresh_token")
    if not access:
        return {"inserted": 0, "skipped": "no_oauth_token"}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=20) as client:
            headers = {"Authorization": f"Bearer {access}"}
            params = {"maxResults": 30}
            if row["filter_query"]:
                params["q"] = row["filter_query"]
            r = await client.get(GMAIL_LIST_URL, headers=headers, params=params)
            if r.status_code == 401 and refresh:
                new_access = await _refresh_gmail_token(refresh)
                if new_access:
                    headers["Authorization"] = f"Bearer {new_access}"
                    await _persist_refreshed(row["id"], new_access)
                    r = await client.get(GMAIL_LIST_URL, headers=headers, params=params)
            if r.status_code != 200:
                return {"inserted": 0, "error": f"gmail list HTTP {r.status_code}"}
            data = r.json() or {}
            messages = (data.get("messages") or [])[:30]
            inserted = 0
            for m in messages:
                mid = m.get("id")
                if not mid:
                    continue
                if await _message_exists(row["id"], mid):
                    continue
                mr = await client.get(
                    GMAIL_GET_URL.format(mid),
                    headers=headers,
                    params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
                )
                if mr.status_code != 200:
                    continue
                detail = mr.json()
                parsed = _parse_gmail_detail(detail)
                await _insert_email(row, mid, detail.get("threadId"), parsed)
                inserted += 1
            return {"inserted": inserted, "total_listed": len(messages)}
    except Exception as e:
        logger.warning("gmail sync error: %s", e)
        return {"inserted": 0, "error": str(e)}


def _parse_gmail_detail(detail: dict) -> dict:
    headers_list = (detail.get("payload") or {}).get("headers", [])
    headers = {h.get("name", "").lower(): h.get("value", "") for h in headers_list}
    snippet = detail.get("snippet", "")
    return {
        "subject": headers.get("subject", ""),
        "from_address": headers.get("from", ""),
        "snippet": snippet,
        "body_text": snippet,  # metadata-only; full body requires format=full
        "received_at": _parse_internal_date(detail.get("internalDate")),
    }


def _parse_internal_date(ts_ms: Optional[str]):
    if not ts_ms:
        return None
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc)
    except Exception:
        return None


async def _persist_refreshed(integration_id, new_access: str):
    from utils.db import get_storage
    from utils.crypto import encrypt_or_passthrough
    storage = await get_storage()
    enc, ver = encrypt_or_passthrough(new_access)
    legacy = new_access if ver == 0 else None
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            update email_integrations set
              oauth_access_token = coalesce($2, oauth_access_token),
              oauth_access_token_enc = coalesce($3, oauth_access_token_enc),
              encryption_version = greatest(coalesce(encryption_version,0), $4)
             where id = $1::uuid
            """,
            integration_id, legacy, enc, ver,
        )


async def _sync_outlook(row) -> dict:
    access = _resolve_token(row, "oauth_access_token_enc", "oauth_access_token")
    if not access:
        return {"inserted": 0, "skipped": "no_oauth_token"}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=20) as client:
            headers = {"Authorization": f"Bearer {access}"}
            params = {"$top": "30", "$orderby": "receivedDateTime desc",
                      "$select": "id,subject,from,bodyPreview,receivedDateTime,conversationId,hasAttachments"}
            if row["filter_query"]:
                params["$search"] = f'"{row["filter_query"]}"'
            r = await client.get(GRAPH_MESSAGES_URL, headers=headers, params=params)
            if r.status_code != 200:
                return {"inserted": 0, "error": f"outlook list HTTP {r.status_code}"}
            data = r.json() or {}
            messages = data.get("value") or []
            inserted = 0
            for m in messages:
                mid = m.get("id")
                if not mid or await _message_exists(row["id"], mid):
                    continue
                parsed = {
                    "subject": m.get("subject", ""),
                    "from_address": ((m.get("from") or {}).get("emailAddress") or {}).get("address", ""),
                    "snippet": m.get("bodyPreview", ""),
                    "body_text": m.get("bodyPreview", ""),
                    "received_at": _parse_outlook_date(m.get("receivedDateTime")),
                }
                await _insert_email(row, mid, m.get("conversationId"), parsed)
                inserted += 1
            return {"inserted": inserted, "total_listed": len(messages)}
    except Exception as e:
        logger.warning("outlook sync error: %s", e)
        return {"inserted": 0, "error": str(e)}


def _parse_outlook_date(s: Optional[str]):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


async def _sync_imap(row) -> dict:
    pwd = None
    enc = row["imap_password_enc_v2"]
    if enc:
        from utils.crypto import decrypt
        pwd = decrypt(enc)
    if not pwd:
        pwd = row["imap_password_enc"]
    if not (row["imap_host"] and row["imap_username"] and pwd):
        return {"inserted": 0, "skipped": "incomplete_imap_config"}
    # IMAP es bloqueante; lo ejecutamos en thread.
    def _pull() -> list[dict]:
        import email
        import imaplib
        msgs: list[dict] = []
        port = row["imap_port"] or 993
        m = imaplib.IMAP4_SSL(row["imap_host"], port, timeout=20)
        try:
            m.login(row["imap_username"], pwd)
            m.select(row["watch_label"] or "INBOX")
            typ, data = m.search(None, "ALL")
            if typ != "OK" or not data or not data[0]:
                return msgs
            ids = data[0].split()[-30:]  # últimos 30
            for mid in ids:
                typ, msg_data = m.fetch(mid, "(BODY.PEEK[HEADER] UID)")
                if typ != "OK" or not msg_data:
                    continue
                # Parse first response tuple
                raw = next((p[1] for p in msg_data if isinstance(p, tuple)), None)
                if not raw:
                    continue
                msg = email.message_from_bytes(raw)
                uid = mid.decode() if isinstance(mid, bytes) else str(mid)
                msgs.append({
                    "external_id": uid,
                    "thread_id": msg.get("Message-ID", ""),
                    "subject": msg.get("Subject", ""),
                    "from_address": msg.get("From", ""),
                    "snippet": "",
                    "body_text": "",
                    "received_at_raw": msg.get("Date", ""),
                })
        finally:
            try:
                m.close()
            except Exception:
                pass
            m.logout()
        return msgs
    msgs = await asyncio.to_thread(_pull)
    inserted = 0
    for raw in msgs:
        if await _message_exists(row["id"], raw["external_id"]):
            continue
        from email.utils import parsedate_to_datetime
        try:
            received = parsedate_to_datetime(raw["received_at_raw"]) if raw.get("received_at_raw") else None
        except Exception:
            received = None
        parsed = {
            "subject": raw["subject"],
            "from_address": raw["from_address"],
            "snippet": raw["snippet"],
            "body_text": raw["body_text"],
            "received_at": received,
        }
        await _insert_email(row, raw["external_id"], raw["thread_id"], parsed)
        inserted += 1
    return {"inserted": inserted, "total_listed": len(msgs)}


async def _message_exists(integration_id, external_id: str) -> bool:
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        v = await conn.fetchval(
            "select 1 from email_messages where integration_id = $1::uuid and external_id = $2 limit 1",
            integration_id, external_id,
        )
    return bool(v)


async def _insert_email(row, external_id: str, thread_id: Optional[str], parsed: dict) -> Optional[str]:
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rec = await conn.fetchrow(
            """
            insert into email_messages
              (firm_id, integration_id, external_id, thread_id, from_address,
               subject, snippet, body_text, received_at)
            values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9)
            on conflict (integration_id, external_id) do nothing
            returning id
            """,
            row["firm_id"], row["id"], external_id, thread_id,
            parsed.get("from_address", ""),
            parsed.get("subject", ""),
            parsed.get("snippet", ""),
            parsed.get("body_text", ""),
            parsed.get("received_at"),
        )
    if not rec:
        return None
    msg_id = str(rec["id"])
    # Clasifica con parse_legal_email
    try:
        from agent.tools.parse_legal_email import parse_legal_email_tool
        await parse_legal_email_tool(
            {
                "subject": parsed.get("subject"),
                "body": parsed.get("body_text") or parsed.get("snippet"),
                "from_address": parsed.get("from_address"),
                "email_message_id": msg_id,
            },
            {"firm_id": str(row["firm_id"]), "user_id": str(row["user_id"])},
        )
    except Exception as e:
        logger.debug("parse_legal_email skipped: %s", e)
    return msg_id


# ════════════════════════════════════════════════════════════════════════
# Voice tools
# ════════════════════════════════════════════════════════════════════════

async def sync_email_now_tool(args: dict, ctx: dict) -> dict:
    """Voice tool: 'LexAI, revisa mi correo'."""
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    return await sync_all_for_firm(firm_id)
