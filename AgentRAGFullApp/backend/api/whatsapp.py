"""Sprint 7 · WhatsApp Business Cloud API integration.

Endpoints:
  GET    /v1/whatsapp/integration         · estado de la integración
  POST   /v1/whatsapp/integration         · setup (phone_number_id, waba_id, token)
  DELETE /v1/whatsapp/integration         · desconectar
  POST   /v1/whatsapp/send                · enviar mensaje (texto / plantilla)
  GET    /v1/whatsapp/messages            · historial de conversación
  GET    /v1/whatsapp/messages/by-client/{client_id}
  GET    /v1/whatsapp/webhook             · verify hook (Meta)
  POST   /v1/whatsapp/webhook             · receive inbound (Meta)

Variables de entorno opcionales:
  WHATSAPP_GRAPH_BASE      · default https://graph.facebook.com/v18.0
  WHATSAPP_VERIFY_TOKEN    · si se setea globalmente; sino se lee de la integración

WhatsApp Business Cloud API requiere:
  - phone_number_id (id de Meta Business)
  - waba_id (WhatsApp Business Account)
  - access_token (System User token de Meta)
  - webhook_verify_token (string aleatorio que setea el admin)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/whatsapp", tags=["whatsapp"])


def _graph_base() -> str:
    return os.getenv("WHATSAPP_GRAPH_BASE", "https://graph.facebook.com/v18.0")


# ──────────────────────────────────────────────────────────────────────
# Integration CRUD
# ──────────────────────────────────────────────────────────────────────


class SetupRequest(BaseModel):
    phone_number_id: str = Field(min_length=3)
    display_phone: Optional[str] = None
    waba_id: Optional[str] = None
    access_token: str = Field(min_length=10)
    webhook_verify_token: str = Field(min_length=6)


@router.get("/integration")
async def get_integration(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, phone_number_id, display_phone, waba_id, active, status,
                   last_status, last_error, created_at, updated_at
              from whatsapp_integrations where firm_id = $1::uuid
            """,
            principal.firm_id,
        )
    if not row:
        return {"configured": False}
    return {
        "configured": True,
        "id": str(row["id"]),
        "phone_number_id": row["phone_number_id"],
        "display_phone": row["display_phone"],
        "waba_id": row["waba_id"],
        "active": row["active"],
        "status": row["status"],
        "last_status": row["last_status"],
        "last_error": row["last_error"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


@router.post("/integration")
async def setup_integration(
    body: SetupRequest,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin / socio_senior puede configurar WhatsApp")
    from utils.db import get_storage
    from utils.crypto import encrypt_or_passthrough
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    enc, ver = encrypt_or_passthrough(body.access_token)
    legacy = body.access_token if ver == 0 else None
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into whatsapp_integrations
              (firm_id, phone_number_id, display_phone, waba_id,
               access_token, access_token_enc, encryption_version,
               webhook_verify_token, status)
            values ($1::uuid, $2, $3, $4, $5, $6, $7, $8, 'connected')
            on conflict (firm_id) do update set
              phone_number_id = excluded.phone_number_id,
              display_phone = excluded.display_phone,
              waba_id = excluded.waba_id,
              access_token = excluded.access_token,
              access_token_enc = excluded.access_token_enc,
              encryption_version = excluded.encryption_version,
              webhook_verify_token = excluded.webhook_verify_token,
              active = true, status = 'connected', updated_at = now()
            returning id
            """,
            principal.firm_id, body.phone_number_id, body.display_phone,
            body.waba_id, legacy, enc, ver, body.webhook_verify_token,
        )
    return {"id": str(row["id"]), "ok": True}


@router.delete("/integration")
async def delete_integration(principal: Principal = Depends(get_current_firm)):
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "delete from whatsapp_integrations where firm_id = $1::uuid",
            principal.firm_id,
        )
    return {"deleted": True}


# ──────────────────────────────────────────────────────────────────────
# Send message
# ──────────────────────────────────────────────────────────────────────


class SendRequest(BaseModel):
    to_phone: str = Field(min_length=8, max_length=20)
    body: Optional[str] = None
    template_name: Optional[str] = None
    template_params: Optional[list[str]] = None
    client_id: Optional[str] = None
    matter_id: Optional[str] = None


@router.post("/send")
async def send_message(
    body: SendRequest,
    principal: Principal = Depends(get_current_firm),
):
    if not (body.body or body.template_name):
        raise HTTPException(400, "body o template_name requerido")
    from utils.db import get_storage
    from utils.crypto import decrypt
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        integ = await conn.fetchrow(
            """
            select id, phone_number_id, access_token, access_token_enc
              from whatsapp_integrations
             where firm_id = $1::uuid and active = true
            """,
            principal.firm_id,
        )
    if not integ:
        raise HTTPException(404, "WhatsApp no configurado en esta firma")
    token = (decrypt(integ["access_token_enc"]) if integ["access_token_enc"] else None) or integ["access_token"]
    if not token:
        raise HTTPException(503, "access_token no disponible")

    payload: dict = {
        "messaging_product": "whatsapp",
        "to": body.to_phone,
        "recipient_type": "individual",
    }
    if body.template_name:
        payload["type"] = "template"
        components = []
        if body.template_params:
            components = [{
                "type": "body",
                "parameters": [{"type": "text", "text": p} for p in body.template_params],
            }]
        payload["template"] = {
            "name": body.template_name,
            "language": {"code": "es_CO"},
            "components": components,
        }
    else:
        payload["type"] = "text"
        payload["text"] = {"body": body.body or ""}

    import httpx
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{_graph_base()}/{integ['phone_number_id']}/messages",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=payload,
            )
            if r.status_code not in (200, 201):
                raise HTTPException(502, f"WA HTTP {r.status_code}: {r.text[:200]}")
            resp = r.json() or {}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"WA error: {e}")

    wa_id = ((resp.get("messages") or [{}])[0] or {}).get("id", "")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            insert into whatsapp_messages
              (firm_id, integration_id, client_id, matter_id, wa_message_id,
               direction, to_phone, body, template_name, status, raw)
            values ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5,
                    'outbound', $6, $7, $8, 'sent', $9::jsonb)
            on conflict (integration_id, wa_message_id) do nothing
            """,
            principal.firm_id, integ["id"], body.client_id, body.matter_id,
            wa_id, body.to_phone, body.body, body.template_name,
            json.dumps(resp),
        )
    return {"ok": True, "wa_message_id": wa_id, "response": resp}


# ──────────────────────────────────────────────────────────────────────
# History
# ──────────────────────────────────────────────────────────────────────


@router.get("/messages")
async def list_messages(
    limit: int = Query(default=50, le=200),
    direction: Optional[str] = Query(default=None, regex="^(inbound|outbound)$"),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    where = ["firm_id = $1::uuid"]
    params: list = [principal.firm_id]
    if direction:
        params.append(direction); where.append(f"direction = ${len(params)}")
    params.append(limit)
    sql = f"""
        select id, direction, from_phone, to_phone, body, template_name,
               status, client_id, matter_id, occurred_at
          from whatsapp_messages
         where {' and '.join(where)}
         order by occurred_at desc
         limit ${len(params)}
    """
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]),
                "direction": r["direction"],
                "from_phone": r["from_phone"],
                "to_phone": r["to_phone"],
                "body": r["body"],
                "template_name": r["template_name"],
                "status": r["status"],
                "client_id": str(r["client_id"]) if r["client_id"] else None,
                "matter_id": str(r["matter_id"]) if r["matter_id"] else None,
                "occurred_at": r["occurred_at"].isoformat() if r["occurred_at"] else None,
            }
            for r in rows
        ],
    }


@router.get("/messages/by-client/{client_id}")
async def messages_by_client(
    client_id: str,
    limit: int = Query(default=50, le=200),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, direction, from_phone, to_phone, body, status, occurred_at
              from whatsapp_messages
             where firm_id = $1::uuid and client_id = $2::uuid
             order by occurred_at desc limit $3
            """,
            principal.firm_id, client_id, limit,
        )
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]),
                "direction": r["direction"],
                "from_phone": r["from_phone"],
                "to_phone": r["to_phone"],
                "body": r["body"],
                "status": r["status"],
                "occurred_at": r["occurred_at"].isoformat() if r["occurred_at"] else None,
            }
            for r in rows
        ],
    }


# ──────────────────────────────────────────────────────────────────────
# Webhook (Meta)
# ──────────────────────────────────────────────────────────────────────


@router.get("/webhook")
async def verify_webhook(request: Request):
    """Meta envía GET con hub.mode=subscribe + hub.challenge + hub.verify_token.
    Si verify_token coincide con el de alguna integración activa, devolvemos el challenge."""
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode != "subscribe" or not token:
        return PlainTextResponse("bad request", status_code=400)
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return PlainTextResponse("no storage", status_code=503)
    async with storage.pool.acquire() as conn:
        ok = await conn.fetchval(
            "select 1 from whatsapp_integrations where webhook_verify_token = $1 and active = true",
            token,
        )
    if not ok:
        return PlainTextResponse("forbidden", status_code=403)
    return PlainTextResponse(challenge or "ok")


@router.post("/webhook")
async def receive_webhook(request: Request):
    """Recibe eventos de Meta. NO auth standard (es público), pero validamos
    que el payload provenga de una integración con waba_id conocido."""
    try:
        payload = await request.json()
    except Exception:
        return {"ok": False, "reason": "bad_json"}

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"ok": False, "reason": "no_storage"}

    entries = payload.get("entry") or []
    inserted = 0
    for entry in entries:
        waba_id = entry.get("id")
        async with storage.pool.acquire() as conn:
            integ = await conn.fetchrow(
                "select id, firm_id, phone_number_id from whatsapp_integrations where waba_id = $1 limit 1",
                waba_id,
            )
        if not integ:
            continue
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            messages = value.get("messages") or []
            for m in messages:
                wa_id = m.get("id")
                from_phone = m.get("from")
                body_text = ((m.get("text") or {}).get("body") or "")
                async with storage.pool.acquire() as conn:
                    rec = await conn.fetchrow(
                        """
                        insert into whatsapp_messages
                          (firm_id, integration_id, wa_message_id, direction,
                           from_phone, to_phone, body, raw)
                        values ($1::uuid, $2::uuid, $3, 'inbound', $4, $5, $6, $7::jsonb)
                        on conflict (integration_id, wa_message_id) do nothing
                        returning id
                        """,
                        integ["firm_id"], integ["id"], wa_id,
                        from_phone, integ["phone_number_id"], body_text,
                        json.dumps(m),
                    )
                if rec:
                    inserted += 1
                    # Match cliente por teléfono (best-effort)
                    try:
                        async with storage.pool.acquire() as conn:
                            client = await conn.fetchrow(
                                """
                                select id from clients
                                 where firm_id = $1::uuid
                                   and (telefono = $2 or replace(telefono,' ','') = $2)
                                 limit 1
                                """,
                                integ["firm_id"], from_phone,
                            )
                        if client:
                            async with storage.pool.acquire() as conn:
                                await conn.execute(
                                    "update whatsapp_messages set client_id = $2::uuid where id = $1::uuid",
                                    rec["id"], client["id"],
                                )
                    except Exception:
                        pass
    return {"ok": True, "inserted": inserted}


# ════════════════════════════════════════════════════════════════════════
# Voice tool
# ════════════════════════════════════════════════════════════════════════


async def send_whatsapp_tool(args: dict, ctx: dict) -> dict:
    """Voice tool: 'LexAI, envía WhatsApp a Juan diciéndole ...'."""
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    to = (args.get("to_phone") or "").strip()
    body = (args.get("body") or "").strip()
    if not (to and body):
        return {"error": "to_phone y body requeridos"}
    from utils.db import get_storage
    from utils.crypto import decrypt
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}
    async with storage.pool.acquire() as conn:
        integ = await conn.fetchrow(
            """select id, phone_number_id, access_token, access_token_enc
                from whatsapp_integrations
               where firm_id = $1::uuid and active = true""",
            firm_id,
        )
    if not integ:
        return {"error": "WhatsApp no configurado"}
    token = (decrypt(integ["access_token_enc"]) if integ["access_token_enc"] else None) or integ["access_token"]
    if not token:
        return {"error": "token no disponible"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to, "recipient_type": "individual",
        "type": "text", "text": {"body": body},
    }
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{_graph_base()}/{integ['phone_number_id']}/messages",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=payload,
            )
            if r.status_code not in (200, 201):
                return {"error": f"WA HTTP {r.status_code}"}
            resp = r.json() or {}
    except Exception as e:
        return {"error": str(e)}
    wa_id = ((resp.get("messages") or [{}])[0] or {}).get("id", "")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """insert into whatsapp_messages
                 (firm_id, integration_id, wa_message_id, direction, to_phone, body, status, raw)
               values ($1::uuid, $2::uuid, $3, 'outbound', $4, $5, 'sent', $6::jsonb)
               on conflict (integration_id, wa_message_id) do nothing""",
            firm_id, integ["id"], wa_id, to, body, json.dumps(resp),
        )
    return {"ok": True, "wa_message_id": wa_id}
