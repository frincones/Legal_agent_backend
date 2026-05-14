"""Sprint 14 · Webhook dispatcher worker.

Procesa webhook_deliveries en status='pending'/'retrying' que tengan
next_retry_at <= now(). Hace POST al endpoint con HMAC sign en header.

Reintentos:
  attempt 1: inmediato
  attempt 2: +2 min
  attempt 3: +10 min
  attempt 4: +1 hora
  attempt 5: +6 horas
  attempt > 5: status='failed'

Punto de entrada:
  POST /v1/webhooks/dispatch-now (admin)
  Cron Railway invoca el endpoint cada N minutos.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


RETRY_BACKOFF_MINUTES = [0, 2, 10, 60, 360]  # 5 intentos


async def dispatch_pending(limit: int = 50) -> dict:
    """Procesa hasta N entregas pendientes/retrying."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage unavailable"}

    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select d.id, d.firm_id, d.webhook_id, d.event_type, d.event_id,
                   d.payload, d.attempt_count, d.max_attempts,
                   w.url, w.secret, w.active
              from webhook_deliveries d
              join outbound_webhooks w on w.id = d.webhook_id
             where d.status in ('pending','retrying')
               and (d.next_retry_at is null or d.next_retry_at <= now())
               and w.active = true
             order by d.next_retry_at asc nulls first
             limit $1
            """,
            limit,
        )

    sent, failed = 0, 0
    for r in rows:
        result = await _attempt_delivery(r)
        if result["success"]:
            sent += 1
        else:
            failed += 1
    return {"processed": len(rows), "sent": sent, "failed": failed}


async def _attempt_delivery(row) -> dict:
    import httpx
    from utils.db import get_storage
    storage = await get_storage()

    body_bytes = json.dumps(row["payload"]).encode("utf-8")
    signature_header = _sign(row["secret"], body_bytes)
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "LexAI-Webhook/1.0",
        "X-LexAI-Event": row["event_type"],
        "X-LexAI-Event-Id": row["event_id"],
        "X-LexAI-Timestamp": timestamp,
        "X-LexAI-Signature": signature_header,
    }
    status_code: Optional[int] = None
    response_text: Optional[str] = None
    err: Optional[str] = None
    success = False
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.post(row["url"], headers=headers, content=body_bytes)
            status_code = resp.status_code
            response_text = resp.text[:1000]
            success = 200 <= status_code < 300
    except Exception as e:
        err = str(e)[:500]

    attempt = (row["attempt_count"] or 0) + 1
    if success:
        async with storage.pool.acquire() as conn:
            await conn.execute(
                """
                update webhook_deliveries set
                  status = 'succeeded', status_code = $2, response_body = $3,
                  attempt_count = $4, succeeded_at = now()
                 where id = $1::uuid
                """,
                row["id"], status_code, response_text, attempt,
            )
            await conn.execute(
                """
                update outbound_webhooks set
                  success_count = success_count + 1,
                  last_delivery_at = now(), last_status_code = $2
                 where id = $1::uuid
                """,
                row["webhook_id"], status_code,
            )
    else:
        # ¿Reintentar?
        if attempt >= (row["max_attempts"] or 5):
            new_status = "failed"
            next_retry = None
            failed_at = datetime.now(timezone.utc)
        else:
            new_status = "retrying"
            minutes = RETRY_BACKOFF_MINUTES[min(attempt, len(RETRY_BACKOFF_MINUTES) - 1)]
            next_retry = datetime.now(timezone.utc) + timedelta(minutes=minutes)
            failed_at = None
        async with storage.pool.acquire() as conn:
            await conn.execute(
                """
                update webhook_deliveries set
                  status = $2, status_code = $3, response_body = $4,
                  attempt_count = $5, next_retry_at = $6::timestamptz,
                  failed_at = $7::timestamptz, error = $8
                 where id = $1::uuid
                """,
                row["id"], new_status, status_code, response_text,
                attempt, next_retry, failed_at, err,
            )
            await conn.execute(
                """
                update outbound_webhooks set
                  failure_count = failure_count + 1,
                  last_delivery_at = now(), last_status_code = $2
                 where id = $1::uuid
                """,
                row["webhook_id"], status_code,
            )
    return {"success": success, "delivery_id": str(row["id"]), "status_code": status_code, "error": err}


def _sign(secret: str, body: bytes) -> str:
    """HMAC-SHA256 hex."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
