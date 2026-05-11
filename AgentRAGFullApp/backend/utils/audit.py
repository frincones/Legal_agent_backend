"""Sprint 6 · Audit logging helper.

Escribe filas a `audit_logs` para cumplir Habeas Data (Ley 1581/2012 CO):
  - quién accedió a información personal
  - cuándo
  - desde dónde (IP + UA)
  - con qué resultado

Diseñado para no romper en caso de fallo: nunca lanza excepciones que
afecten la operación real (los inserts son fire-and-forget detrás de un
try/except).

Uso:
    from utils.audit import audit_log
    await audit_log(
        firm_id=firm, user_id=user,
        action="client.read", resource_type="client", resource_id=cid,
        data_subject_id=cliente.nit,
    )
"""

from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def audit_log(
    firm_id: Optional[str],
    user_id: Optional[str],
    action: str,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    data_subject_id: Optional[str] = None,
    outcome: str = "success",
    reason: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    if not firm_id:
        return
    try:
        from utils.db import get_storage
        storage = await get_storage()
        if not hasattr(storage, "pool"):
            return
        async with storage.pool.acquire() as conn:
            await conn.execute(
                """
                insert into audit_logs
                  (firm_id, user_id, action, resource_type, resource_id,
                   ip_address, user_agent, outcome, reason, data_subject_id, metadata)
                values
                  ($1::uuid, $2::uuid, $3, $4, $5,
                   $6::inet, $7, $8, $9, $10, $11::jsonb)
                """,
                firm_id, user_id, action, resource_type, resource_id,
                ip_address, user_agent, outcome, reason, data_subject_id,
                json.dumps(metadata or {}),
            )
    except Exception as e:
        logger.warning("audit_log failed: %s", e)


def audit_from_request(request) -> dict:
    """Extrae IP + User-Agent del Request de FastAPI."""
    try:
        ip = request.client.host if request.client else None
        ua = request.headers.get("user-agent")
        # X-Forwarded-For wins (Railway, Vercel preserve esto)
        xff = request.headers.get("x-forwarded-for")
        if xff:
            ip = xff.split(",")[0].strip()
        return {"ip_address": ip, "user_agent": ua}
    except Exception:
        return {}


async def track_usage(
    firm_id: Optional[str],
    user_id: Optional[str],
    kind: str,
    count: int = 1,
    cost_units: float = 0.0,
    metadata: Optional[dict] = None,
) -> None:
    """Escribe a usage_events. Usado para billing y quotas."""
    if not firm_id:
        return
    try:
        from utils.db import get_storage
        storage = await get_storage()
        if not hasattr(storage, "pool"):
            return
        async with storage.pool.acquire() as conn:
            await conn.execute(
                """
                insert into usage_events (firm_id, user_id, kind, count, cost_units, metadata)
                values ($1::uuid, $2::uuid, $3, $4, $5, $6::jsonb)
                """,
                firm_id, user_id, kind, count, cost_units,
                json.dumps(metadata or {}),
            )
    except Exception as e:
        logger.debug("track_usage failed: %s", e)
