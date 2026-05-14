"""Sprint 14 · Event emitter para webhooks salientes.

Helper de uso:
    from utils.event_emitter import emit_event
    await emit_event(firm_id, "matter.created", {"id": ..., "titulo": ...})

Encola un webhook_delivery por cada outbound_webhook activo de la firma
que esté suscrito al event_type. Un worker separado (webhook_dispatcher)
hace la entrega HTTP.

Eventos soportados (sugerencia, no enforced):
  matter.created · matter.status_changed · matter.archived
  client.created · client.updated
  lead.created · lead.converted · lead.stage_changed
  deadline.due_soon · deadline.completed
  invoice.sent · invoice.paid
  signature.signed · signature.declined
  contract.analyzed · insight.created
"""

from __future__ import annotations

import json
import logging
import secrets

logger = logging.getLogger(__name__)


async def emit_event(firm_id: str, event_type: str, payload: dict) -> dict:
    """Enqueue delivery a todos los webhooks suscritos. Fire-and-forget."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"enqueued": 0, "skipped": "no_storage"}

    async with storage.pool.acquire() as conn:
        webhooks = await conn.fetch(
            """
            select id from outbound_webhooks
             where firm_id = $1::uuid and active = true
               and ($2 = any(events) or 'all' = any(events))
            """,
            firm_id, event_type,
        )
        if not webhooks:
            return {"enqueued": 0}

        event_id = secrets.token_urlsafe(16)
        enqueued = 0
        for w in webhooks:
            try:
                await conn.execute(
                    """
                    insert into webhook_deliveries
                      (firm_id, webhook_id, event_type, event_id, payload,
                       status, next_retry_at)
                    values ($1::uuid, $2::uuid, $3, $4, $5::jsonb, 'pending', now())
                    on conflict (webhook_id, event_id) do nothing
                    """,
                    firm_id, w["id"], event_type, event_id, json.dumps(payload),
                )
                enqueued += 1
            except Exception as e:
                logger.debug("emit_event insert skipped: %s", e)
    return {"enqueued": enqueued, "event_id": event_id, "event_type": event_type}
