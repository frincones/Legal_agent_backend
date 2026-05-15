"""Sprint B/C · Endpoint admin invocado por pg_cron + pg_net.

Dispara los workers de sync periódicos:
  - type=calendar  → calendar_sync (delta sync · Google syncToken + Outlook deltaLink)
  - type=cloud     → cloud_sync (Sprint C · Drive + OneDrive + Dropbox watchers)

Protegido con X-Cron-Secret header. NO usa JWT auth porque viene de
Postgres pg_net.

Idempotente: si lo invocan en paralelo, los workers son tolerantes
(advisory locks por integration_id).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/admin", tags=["admin"])

CRON_SECRET = os.getenv("CRON_SECRET", "")


def _verify_cron(secret_header: Optional[str]):
    if not CRON_SECRET:
        logger.warning("CRON_SECRET not configured; allowing sync-tick (dev mode)")
        return
    if secret_header != CRON_SECRET:
        raise HTTPException(401, "Invalid cron secret")


@router.post("/sync-tick")
async def sync_tick(
    type: str = Query(..., regex="^(calendar|cloud|tokens)$"),
    x_cron_secret: Optional[str] = Header(default=None, alias="X-Cron-Secret"),
):
    """Dispara el worker correspondiente."""
    _verify_cron(x_cron_secret)

    if type == "calendar":
        return await _tick_calendar()
    if type == "cloud":
        return await _tick_cloud()
    if type == "tokens":
        return await _tick_tokens()
    raise HTTPException(400, f"Unknown sync type: {type}")


async def _tick_calendar() -> dict:
    """Llama calendar_sync para todas las integraciones activas."""
    try:
        from agent.workers.calendar_sync import sync_all_active
        result = await sync_all_active()
        return {"ok": True, "type": "calendar", "result": result}
    except Exception as e:
        logger.exception("calendar sync tick failed")
        return {"ok": False, "type": "calendar", "error": str(e)[:200]}


async def _tick_cloud() -> dict:
    """Sprint C · cloud_sync."""
    try:
        from agent.workers.cloud_sync import sync_all_watchers  # type: ignore
        result = await sync_all_watchers()
        return {"ok": True, "type": "cloud", "result": result}
    except ImportError:
        # Sprint C not yet built
        return {"ok": False, "type": "cloud", "skipped": "worker_not_built"}
    except Exception as e:
        logger.exception("cloud sync tick failed")
        return {"ok": False, "type": "cloud", "error": str(e)[:200]}


async def _tick_tokens() -> dict:
    """Refresca tokens próximos a expirar."""
    try:
        from utils.db import get_storage
        from agent.workers.token_refresh import refresh_due_tokens
        storage = await get_storage()
        result = await refresh_due_tokens(storage.pool, window_minutes=10)
        return {"ok": True, "type": "tokens", "result": result}
    except Exception as e:
        logger.exception("tokens refresh tick failed")
        return {"ok": False, "type": "tokens", "error": str(e)[:200]}
