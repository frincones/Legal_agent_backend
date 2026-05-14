"""Sprint 23 · Billing worker · period rollover + trial expiry.

Tareas:
  - reset_counters_if_new_month(): cuando el counter del mes en curso no
    existe, no hay nada que resetear (cada mes arranca con 0 implícito).
    Pero corremos un check: si una firm tiene current_period_end < now()
    y status='active', la pasamos a 'past_due' (Paddle nos lo confirmará
    via webhook, pero sirve como salvaguarda).
  - expire_trials(): firms con status='trialing' y trial_ends_at < now()
    pasan a 'past_due' (para que el frontend muestre la UpgradeModal).

Punto de entrada:
  POST /v1/billing/admin/run-worker  · admin-only (también vía cron Railway).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def expire_trials() -> dict[str, Any]:
    """Marca como past_due las suscripciones con trial vencido.

    No toca planes pagos (esos son responsabilidad de Paddle via webhook).
    """
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"ok": False, "error": "no_storage"}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            update firm_subscriptions
               set status = 'past_due', updated_at = now()
             where status = 'trialing'
               and trial_ends_at is not null
               and trial_ends_at < now()
               and plan_code = 'free'
             returning firm_id
            """,
        )
    return {"ok": True, "expired_count": len(rows), "firm_ids": [str(r["firm_id"]) for r in rows]}


async def rollover_periods() -> dict[str, Any]:
    """Cuando current_period_end ya pasó y status='active' (no Paddle-managed),
    actualiza al siguiente mes. Solo para planes free (Paddle gestiona los pagos)."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"ok": False, "error": "no_storage"}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            update firm_subscriptions
               set current_period_start = date_trunc('month', now()),
                   current_period_end = date_trunc('month', now()) + interval '1 month',
                   updated_at = now()
             where status in ('active','trialing')
               and current_period_end < now()
               and paddle_subscription_id is null
             returning firm_id, plan_code
            """,
        )
    return {"ok": True, "rolled_count": len(rows), "details": [dict(r) for r in rows]}


async def run_all() -> dict[str, Any]:
    """Corre todas las tareas del billing worker."""
    expire = await expire_trials()
    rollover = await rollover_periods()
    return {"expire_trials": expire, "rollover_periods": rollover}
