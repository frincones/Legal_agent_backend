"""Sprint M21.S8.B · Usage meters + admin audit helper."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

logger = logging.getLogger(__name__)


async def record_usage(
    pool, *, firm_id: UUID, resource_type: str,
    count: int = 1, cost_usd: float = 0.0,
) -> None:
    """Incrementa contador del period_month actual + cost_usd acumulado."""
    if pool is None:
        return
    period_month = datetime.now(timezone.utc).strftime("%Y-%m")
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                insert into firm_usage_meters
                    (firm_id, period_month, resource_type, count, cost_usd, last_updated)
                values ($1::uuid, $2, $3, $4, $5, now())
                on conflict (firm_id, period_month, resource_type) do update set
                    count = firm_usage_meters.count + excluded.count,
                    cost_usd = firm_usage_meters.cost_usd + excluded.cost_usd,
                    last_updated = now()
                """,
                str(firm_id), period_month, resource_type, count, float(cost_usd),
            )
    except Exception as e:
        logger.debug("record_usage failed (non-fatal): %s", e)


async def admin_audit(
    pool, *, event_kind: str, firm_id: Optional[UUID] = None,
    actor_user_id: Optional[UUID] = None, actor_role: Optional[str] = None,
    ip_address: Optional[str] = None, user_agent: Optional[str] = None,
    target_resource: Optional[str] = None, summary: Optional[str] = None,
    details: Optional[dict] = None,
) -> None:
    """Append-only audit centralizado para eventos administrativos."""
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                insert into admin_audit_events
                    (firm_id, event_kind, actor_user_id, actor_role,
                     ip_address, user_agent, target_resource, summary, details)
                values ($1, $2, $3, $4, $5::inet, $6, $7, $8, $9::jsonb)
                """,
                str(firm_id) if firm_id else None,
                event_kind,
                str(actor_user_id) if actor_user_id else None,
                actor_role, ip_address, user_agent,
                target_resource,
                (summary or "")[:500],
                json.dumps(details or {}, default=str, ensure_ascii=False),
            )
    except Exception as e:
        logger.debug("admin_audit insert failed: %s", e)


async def get_usage_summary(pool, firm_id: UUID, period_month: Optional[str] = None) -> dict:
    """Resumen de uso del firm para un mes (default = mes actual)."""
    if pool is None:
        return {"firm_id": str(firm_id), "period_month": None, "items": []}
    period_month = period_month or datetime.now(timezone.utc).strftime("%Y-%m")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select resource_type, count, cost_usd, last_updated
              from firm_usage_meters
             where firm_id=$1::uuid and period_month=$2
             order by resource_type
            """,
            str(firm_id), period_month,
        )
    return {
        "firm_id": str(firm_id),
        "period_month": period_month,
        "items": [
            {
                "resource_type": r["resource_type"],
                "count": int(r["count"]),
                "cost_usd": float(r["cost_usd"]),
                "last_updated": r["last_updated"].isoformat() if r["last_updated"] else None,
            }
            for r in rows
        ],
        "total_cost_usd": sum(float(r["cost_usd"]) for r in rows),
    }
