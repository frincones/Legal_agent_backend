"""Sprint M21.S4.E · Scheduler · APScheduler in-process.

Diseno minimalista: una sola tarea master corre cada 60s, lee
firm_background_jobs WHERE enabled=true y enabled cron matches, dispatches
los pendientes. No requiere APScheduler externo (evita dependencia nueva);
usa asyncio.create_task + sleep loop.

Ventajas:
  - 0 dependencias nuevas
  - Funciona bien con N firmas <500 (caso LexAI early-stage)
  - Si el backend escala a multiples replicas, conviene migrar a pg_cron
    o a un worker dedicado (Sprint 8+ hardening)
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

logger = logging.getLogger(__name__)


_scheduler_task: Optional[asyncio.Task] = None
_shutdown_event: Optional[asyncio.Event] = None


def _cron_matches(cron_expr: str, now: datetime) -> bool:
    """Match minimo de cron (minute, hour, dom, month, dow). Cada campo: '*' o numero.

    No usamos croniter para evitar dependencia. Patron suficiente para los crons
    fijos que usamos ("0 3 * * *", "0 4 * * *", "*/15 * * * *", etc.).
    """
    parts = (cron_expr or "").split()
    if len(parts) != 5:
        return False

    def match(field: str, value: int) -> bool:
        if field == "*":
            return True
        if field.startswith("*/"):
            try:
                step = int(field[2:])
                return value % step == 0
            except ValueError:
                return False
        try:
            return int(field) == value
        except ValueError:
            return False

    return (
        match(parts[0], now.minute) and
        match(parts[1], now.hour) and
        match(parts[2], now.day) and
        match(parts[3], now.month) and
        match(parts[4], now.weekday())
    )


async def _scheduler_loop(pool, anthropic_client=None, openai_client=None) -> None:
    """Loop master: cada 60s revisa jobs cron candidatos."""
    from .dispatcher import dispatch_agent

    logger.info("agents.scheduler: loop iniciado (interval=60s)")
    last_minute = -1

    while True:
        if _shutdown_event and _shutdown_event.is_set():
            logger.info("agents.scheduler: shutdown")
            return
        try:
            now = datetime.now(timezone.utc)
            # Solo evalua una vez por minuto
            if now.minute == last_minute:
                await asyncio.sleep(15)
                continue
            last_minute = now.minute

            if pool is None:
                await asyncio.sleep(30)
                continue

            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    select job_id, firm_id, agent_name, schedule_cron, config
                      from firm_background_jobs
                     where enabled = true and schedule_cron is not null
                    """,
                )
            candidates = [r for r in rows if r["schedule_cron"] and _cron_matches(r["schedule_cron"], now)]
            if candidates:
                logger.info("agents.scheduler: %d cron jobs matched en %s", len(candidates), now.strftime("%H:%M"))
            for row in candidates:
                try:
                    asyncio.create_task(dispatch_agent(
                        name=row["agent_name"],
                        firm_id=UUID(str(row["firm_id"])),
                        pool=pool, anthropic_client=anthropic_client, openai_client=openai_client,
                        trigger_kind="cron",
                        job_id=UUID(str(row["job_id"])),
                        config=dict(row["config"] or {}),
                    ))
                except Exception as e:
                    logger.warning("agents.scheduler: dispatch failed for %s: %s", row["agent_name"], e)
        except Exception as e:
            logger.exception("agents.scheduler: loop error: %s", e)
        await asyncio.sleep(60)


def start_scheduler(pool, anthropic_client=None, openai_client=None) -> None:
    """Lanza el loop master. Llamar desde lifespan del FastAPI."""
    global _scheduler_task, _shutdown_event
    if _scheduler_task is not None:
        logger.info("agents.scheduler: ya iniciado, skip")
        return
    _shutdown_event = asyncio.Event()
    _scheduler_task = asyncio.create_task(_scheduler_loop(pool, anthropic_client, openai_client))
    logger.info("agents.scheduler: start_scheduler OK")


async def stop_scheduler() -> None:
    global _scheduler_task, _shutdown_event
    if _shutdown_event:
        _shutdown_event.set()
    if _scheduler_task:
        try:
            await asyncio.wait_for(_scheduler_task, timeout=5.0)
        except asyncio.TimeoutError:
            _scheduler_task.cancel()
    _scheduler_task = None
    _shutdown_event = None
    logger.info("agents.scheduler: stopped")


async def emit_event(
    pool, *, firm_id: UUID, event_name: str,
    anthropic_client=None, openai_client=None,
    config_override: Optional[dict] = None,
) -> list[str]:
    """Dispatcha todos los agents trigger=event para este evento."""
    from .dispatcher import dispatch_agent
    from .registry import get_agent_registry

    registry = get_agent_registry()
    fired = []
    for agent in registry.list():
        if agent.trigger_kind != "event":
            continue
        if agent.default_event != event_name:
            continue
        # Busca config del firm para este agent
        cfg = config_override or {}
        if pool is not None:
            try:
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        """
                        select job_id, enabled, config from firm_background_jobs
                         where firm_id=$1 and agent_name=$2 limit 1
                        """,
                        str(firm_id), agent.name,
                    )
                if row and not row["enabled"]:
                    continue
                if row:
                    cfg = {**(dict(row["config"] or {})), **cfg}
                    job_id = UUID(str(row["job_id"]))
                else:
                    job_id = None
            except Exception:
                job_id = None
        else:
            job_id = None
        asyncio.create_task(dispatch_agent(
            name=agent.name, firm_id=firm_id, pool=pool,
            anthropic_client=anthropic_client, openai_client=openai_client,
            trigger_kind="event", job_id=job_id, config=cfg,
        ))
        fired.append(agent.name)
    return fired
