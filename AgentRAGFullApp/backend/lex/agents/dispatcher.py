"""Sprint M21.S4.E · Dispatcher · ejecuta un agent con audit + timeout."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Literal, Optional
from uuid import UUID, uuid4

from .base import AgentContext, AgentRunResult
from .registry import get_agent_registry

logger = logging.getLogger(__name__)


async def dispatch_agent(
    name: str, *, firm_id: UUID,
    pool, anthropic_client=None, openai_client=None,
    trigger_kind: Literal["cron", "event", "manual"] = "manual",
    job_id: Optional[UUID] = None,
    config: Optional[dict] = None,
) -> AgentRunResult:
    """Lanza el agent `name` para el firm `firm_id` con audit completo."""
    registry = get_agent_registry()
    agent = registry.get(name)
    if agent is None:
        logger.warning("dispatch_agent: agent %r not found", name)
        return AgentRunResult(status="error", error_message=f"agent_not_found:{name}")

    run_id = uuid4()
    ctx = AgentContext(
        run_id=run_id, firm_id=firm_id, job_id=job_id,
        trigger_kind=trigger_kind, pool=pool,
        anthropic_client=anthropic_client, openai_client=openai_client,
        config=config or {},
    )

    # Insert run log row (status=running)
    started_at = time.time()
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    insert into agent_run_logs
                        (run_id, firm_id, agent_name, job_id, trigger_kind, status)
                    values ($1::uuid, $2::uuid, $3, $4, $5, 'running')
                    """,
                    str(run_id), str(firm_id), name,
                    str(job_id) if job_id else None, trigger_kind,
                )
        except Exception as e:
            logger.warning("dispatch_agent: insert run log failed: %s", e)

    # Execute with timeout
    try:
        result = await asyncio.wait_for(agent.run(ctx), timeout=agent.timeout_seconds)
    except asyncio.TimeoutError:
        result = AgentRunResult(status="timeout", error_message=f"timeout after {agent.timeout_seconds}s")
    except Exception as e:
        logger.exception("dispatch_agent: agent %r failed", name)
        result = AgentRunResult(status="error", error_message=f"{type(e).__name__}: {e}")

    duration_ms = int((time.time() - started_at) * 1000)

    # Update run log
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    update agent_run_logs set
                        finished_at = now(), duration_ms = $1, status = $2,
                        items_processed = $3, items_succeeded = $4, items_failed = $5,
                        output_summary = $6, error_message = $7,
                        cost_usd = $8, metadata = $9::jsonb
                     where run_id = $10::uuid
                    """,
                    duration_ms, result.status,
                    result.items_processed, result.items_succeeded, result.items_failed,
                    (result.output_summary or "")[:1000],
                    (result.error_message or "")[:500] if result.error_message else None,
                    float(result.cost_usd or 0.0),
                    json.dumps(result.metadata or {}, default=str, ensure_ascii=False),
                    str(run_id),
                )
            # Update firm_background_jobs last_run if applicable
            if job_id:
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        update firm_background_jobs
                           set last_run_at = now(), last_run_status = $1, updated_at = now()
                         where job_id = $2::uuid
                        """,
                        result.status, str(job_id),
                    )
        except Exception as e:
            logger.warning("dispatch_agent: update run log failed: %s", e)

    return result
