"""Sprint M21.S8.A · Rate Limiter sliding-window per firm.

Sin Redis: usa tabla rate_limit_buckets (cleanup diario).
Buckets de 1 minuto por (firm_id, resource_type).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

logger = logging.getLogger(__name__)


DEFAULT_LIMITS = {
    "llm_request":    {"per_minute": 60,  "per_hour": 1000},
    "agent_run":      {"per_minute": 10,  "per_hour": 100},
    "cookbook_run":   {"per_minute": 5,   "per_hour": 50},
    "mcp_call":       {"per_minute": 30,  "per_hour": 500},
    "doc_generate":   {"per_minute": 20,  "per_hour": 200},
    "habeas_export":  {"per_minute": 2,   "per_hour": 10},
}


async def check_and_consume(
    pool, *, firm_id: UUID, resource_type: str,
    overrides: Optional[dict] = None,
) -> dict:
    """Devuelve dict con permitted=bool y remaining counts.

    Si pool is None → permit (failsafe, no bloquea en dev).
    """
    if pool is None:
        return {"permitted": True, "reason": "no_pool", "remaining_minute": None}

    limits = {**DEFAULT_LIMITS.get(resource_type, {"per_minute": 60, "per_hour": 1000}), **(overrides or {})}
    now = datetime.now(timezone.utc)
    window_minute = now.replace(second=0, microsecond=0)
    bucket_minute_id = f"{firm_id}:{resource_type}:{window_minute.strftime('%Y%m%d%H%M')}"

    async with pool.acquire() as conn:
        try:
            count_min = await conn.fetchval(
                """
                insert into rate_limit_buckets (bucket_id, firm_id, resource_type, window_start, count)
                values ($1, $2::uuid, $3, $4, 1)
                on conflict (bucket_id) do update set
                    count = rate_limit_buckets.count + 1,
                    last_request_at = now()
                returning count
                """,
                bucket_minute_id, str(firm_id), resource_type, window_minute,
            )
            # Hour aggregate (last 60 minutes)
            count_hr = await conn.fetchval(
                """
                select coalesce(sum(count), 0)::bigint
                  from rate_limit_buckets
                 where firm_id=$1::uuid and resource_type=$2
                   and window_start > now() - interval '1 hour'
                """,
                str(firm_id), resource_type,
            ) or 0
        except Exception as e:
            logger.warning("rate_limit check failed (failsafe permit): %s", e)
            return {"permitted": True, "reason": f"check_error:{type(e).__name__}", "remaining_minute": None}

    per_min = int(limits.get("per_minute", 60))
    per_hr = int(limits.get("per_hour", 1000))
    if count_min > per_min:
        return {
            "permitted": False, "reason": f"rate_limit_minute:{count_min}/{per_min}",
            "remaining_minute": 0, "remaining_hour": max(0, per_hr - int(count_hr)),
            "resource_type": resource_type,
        }
    if count_hr > per_hr:
        return {
            "permitted": False, "reason": f"rate_limit_hour:{count_hr}/{per_hr}",
            "remaining_minute": max(0, per_min - int(count_min)), "remaining_hour": 0,
            "resource_type": resource_type,
        }

    return {
        "permitted": True,
        "remaining_minute": per_min - int(count_min),
        "remaining_hour": per_hr - int(count_hr),
        "resource_type": resource_type,
    }
