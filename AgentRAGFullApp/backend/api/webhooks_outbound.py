"""Sprint 14 · Outbound webhooks API.

  GET    /v1/webhooks                  · lista
  POST   /v1/webhooks                  · crea
  PATCH  /v1/webhooks/{id}
  DELETE /v1/webhooks/{id}
  POST   /v1/webhooks/{id}/test        · envía evento de prueba
  GET    /v1/webhooks/{id}/deliveries  · log
  POST   /v1/webhooks/dispatch-now     · admin · dispara worker
  GET    /v1/webhooks/events           · catálogo de eventos
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, HttpUrl

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/webhooks", tags=["webhooks_outbound"])


SUPPORTED_EVENTS = [
    "all",
    "matter.created", "matter.status_changed", "matter.archived",
    "client.created", "client.updated",
    "lead.created", "lead.converted", "lead.stage_changed",
    "deadline.due_soon", "deadline.completed",
    "invoice.sent", "invoice.paid",
    "signature.signed", "signature.declined",
    "contract.analyzed", "insight.created",
]


@router.get("/events")
async def list_events():
    return {
        "events": [
            {"name": e, "description": _event_desc(e)}
            for e in SUPPORTED_EVENTS
        ]
    }


def _event_desc(e: str) -> str:
    desc = {
        "all": "Todos los eventos (catch-all)",
        "matter.created": "Nuevo caso creado",
        "matter.status_changed": "Caso cambia de estado",
        "matter.archived": "Caso archivado",
        "client.created": "Cliente registrado",
        "client.updated": "Cliente actualizado",
        "lead.created": "Lead nuevo",
        "lead.converted": "Lead convertido en cliente",
        "lead.stage_changed": "Lead cambia de etapa",
        "deadline.due_soon": "Plazo próximo a vencer",
        "deadline.completed": "Plazo cumplido",
        "invoice.sent": "Factura enviada",
        "invoice.paid": "Factura pagada",
        "signature.signed": "Sobre firmado completamente",
        "signature.declined": "Firmante rechazó",
        "contract.analyzed": "Análisis de contrato listo",
        "insight.created": "Nueva sugerencia IA",
    }
    return desc.get(e, "")


def _serialize(r) -> dict:
    return {
        "id": str(r["id"]), "name": r["name"], "url": r["url"],
        "events": list(r["events"] or []),
        "active": r["active"],
        "last_delivery_at": r["last_delivery_at"].isoformat() if r["last_delivery_at"] else None,
        "last_status_code": r["last_status_code"],
        "success_count": r["success_count"],
        "failure_count": r["failure_count"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


@router.get("")
async def list_webhooks(principal: Principal = Depends(get_current_firm)):
    if principal.role not in ("admin", "socio_senior", "socio_junior"):
        raise HTTPException(403, "Solo socios/admin")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, name, url, events, active, last_delivery_at, last_status_code,
                   success_count, failure_count, created_at
              from outbound_webhooks where firm_id = $1::uuid
             order by created_at desc
            """,
            principal.firm_id,
        )
    return {"count": len(rows), "items": [_serialize(r) for r in rows]}


class CreateRequest(BaseModel):
    name: str = Field(min_length=2)
    url: str = Field(min_length=10)
    events: list[str] = Field(default_factory=lambda: ["all"])
    active: bool = True


@router.post("")
async def create_webhook(
    body: CreateRequest,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin / socio_senior")
    if not (body.url.startswith("http://") or body.url.startswith("https://")):
        raise HTTPException(400, "url debe iniciar con http:// o https://")
    invalid = [e for e in body.events if e not in SUPPORTED_EVENTS]
    if invalid:
        raise HTTPException(400, f"eventos inválidos: {invalid}")

    secret = secrets.token_urlsafe(32)
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into outbound_webhooks
              (firm_id, name, url, secret, events, active, created_by)
            values ($1::uuid, $2, $3, $4, $5::text[], $6, $7::uuid)
            returning id, name, url, events, active, last_delivery_at, last_status_code,
                      success_count, failure_count, created_at
            """,
            principal.firm_id, body.name, body.url, secret,
            body.events, body.active, principal.user_id,
        )
    return {**_serialize(row), "secret": secret, "note": "Guarda el secret · se usa para validar el HMAC en tu endpoint."}


class PatchRequest(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    events: Optional[list[str]] = None
    active: Optional[bool] = None


@router.patch("/{webhook_id}")
async def patch_webhook(
    webhook_id: str,
    body: PatchRequest,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin / socio_senior")
    from utils.db import get_storage
    storage = await get_storage()
    fields, params = [], [webhook_id, principal.firm_id]
    if body.name is not None:
        params.append(body.name); fields.append(f"name = ${len(params)}")
    if body.url is not None:
        if not (body.url.startswith("http://") or body.url.startswith("https://")):
            raise HTTPException(400, "url inválida")
        params.append(body.url); fields.append(f"url = ${len(params)}")
    if body.events is not None:
        invalid = [e for e in body.events if e not in SUPPORTED_EVENTS]
        if invalid:
            raise HTTPException(400, f"eventos inválidos: {invalid}")
        params.append(body.events); fields.append(f"events = ${len(params)}::text[]")
    if body.active is not None:
        params.append(body.active); fields.append(f"active = ${len(params)}")
    if not fields:
        raise HTTPException(400, "nada que actualizar")
    sql = f"""
        update outbound_webhooks set {', '.join(fields)}, updated_at = now()
         where id = $1::uuid and firm_id = $2::uuid
         returning id, name, url, events, active, last_delivery_at, last_status_code,
                   success_count, failure_count, created_at
    """
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(sql, *params)
    if not row:
        raise HTTPException(404, "not found")
    return _serialize(row)


@router.delete("/{webhook_id}")
async def delete_webhook(
    webhook_id: str,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "delete from outbound_webhooks where id = $1::uuid and firm_id = $2::uuid",
            webhook_id, principal.firm_id,
        )
    return {"deleted": True}


@router.post("/{webhook_id}/test")
async def test_webhook(
    webhook_id: str,
    principal: Principal = Depends(get_current_firm),
):
    """Envía un evento de prueba `webhook.test`."""
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        ok = await conn.fetchval(
            "select 1 from outbound_webhooks where id = $1::uuid and firm_id = $2::uuid",
            webhook_id, principal.firm_id,
        )
    if not ok:
        raise HTTPException(404, "not found")

    # Encolar directamente sin pasar por emit_event (forzar a este webhook específico)
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            insert into webhook_deliveries
              (firm_id, webhook_id, event_type, event_id, payload, status, next_retry_at)
            values ($1::uuid, $2::uuid, 'webhook.test', $3, $4::jsonb, 'pending', now())
            """,
            principal.firm_id, webhook_id, secrets.token_urlsafe(16),
            json.dumps({
                "test": True,
                "fired_by": str(principal.user_id) if principal.user_id else None,
                "message": "Test ping desde LexAI",
            }),
        )

    # Dispatch ahora mismo
    from agent.workers.webhook_dispatcher import dispatch_pending
    result = await dispatch_pending(limit=10)
    return {"ok": True, "dispatch": result}


@router.get("/{webhook_id}/deliveries")
async def list_deliveries(
    webhook_id: str,
    limit: int = Query(default=50, le=200),
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior", "socio_junior"):
        raise HTTPException(403, "Solo socios/admin")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, event_type, event_id, status, status_code, attempt_count,
                   max_attempts, next_retry_at, succeeded_at, failed_at, error, created_at
              from webhook_deliveries
             where webhook_id = $1::uuid and firm_id = $2::uuid
             order by created_at desc limit $3
            """,
            webhook_id, principal.firm_id, limit,
        )
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]),
                "event_type": r["event_type"], "event_id": r["event_id"],
                "status": r["status"], "status_code": r["status_code"],
                "attempt_count": r["attempt_count"], "max_attempts": r["max_attempts"],
                "next_retry_at": r["next_retry_at"].isoformat() if r["next_retry_at"] else None,
                "succeeded_at": r["succeeded_at"].isoformat() if r["succeeded_at"] else None,
                "failed_at": r["failed_at"].isoformat() if r["failed_at"] else None,
                "error": r["error"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


@router.post("/dispatch-now")
async def dispatch_now(
    limit: int = Query(default=50, le=200),
    principal: Principal = Depends(get_current_firm),
):
    """Endpoint admin para que Railway cron dispare el worker."""
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin")
    from agent.workers.webhook_dispatcher import dispatch_pending
    return await dispatch_pending(limit)
