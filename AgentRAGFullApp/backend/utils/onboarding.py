"""Sprint 26 · Onboarding helpers.

API:
  - get_state(firm_id) → dict       · snapshot del checklist (RPC)
  - mark_step(firm_id, step, status, metadata) → None  · upsert manual
  - skip_step(firm_id, step) → None · marca como skipped
  - reset_progress(firm_id) → None  · admin
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


async def get_state(firm_id: str) -> dict[str, Any]:
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"steps": [], "progress_pct": 0}
    async with storage.pool.acquire() as conn:
        result = await conn.fetchval("select lexai_onboarding_state($1::uuid)", firm_id)
    return result or {"steps": [], "progress_pct": 0}


async def mark_step(
    firm_id: str,
    step_key: str,
    status: str = "completed",
    metadata: Optional[dict] = None,
) -> bool:
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return False
    try:
        async with storage.pool.acquire() as conn:
            await conn.execute(
                """
                insert into onboarding_progress (firm_id, step_key, status, completed_at, metadata, updated_at)
                values ($1::uuid, $2, $3, case when $3 = 'completed' then now() else null end, $4::jsonb, now())
                on conflict (firm_id, step_key) do update set
                  status = excluded.status,
                  completed_at = case when excluded.status = 'completed' then now() else null end,
                  metadata = excluded.metadata,
                  updated_at = now()
                """,
                firm_id, step_key, status, json.dumps(metadata or {}),
            )
        return True
    except Exception as e:
        logger.warning("onboarding.mark_step error firm=%s step=%s: %s", firm_id, step_key, e)
        return False


async def seed_demo_data(firm_id: str) -> dict[str, Any]:
    """Llama el RPC que siembra clientes + matters demo. Idempotente."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"seeded": False, "error": "no_storage"}
    async with storage.pool.acquire() as conn:
        result = await conn.fetchval("select lexai_seed_demo_data($1::uuid)", firm_id)
    return result or {"seeded": False}


async def list_helper_tips(
    route: Optional[str] = None,
    module_key: Optional[str] = None,
    limit: int = 5,
) -> list[dict]:
    """Devuelve tips contextuales para una ruta/módulo.

    Match priority:
      1. route_pattern exacto
      2. route_pattern con LIKE (ej. '/casos/%')
      3. module_key match
      4. globales (route_pattern is null and module_key is null)
    """
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return []
    where_clauses = ["active = true"]
    params: list = []
    if route or module_key:
        # OR logic
        ors = []
        if route:
            params.append(route)
            ors.append(f"route_pattern = ${len(params)}")
            params.append(route)
            ors.append(f"(route_pattern like '%\\%' and ${len(params)} like replace(route_pattern, '%', '%'))")
        if module_key:
            params.append(module_key)
            ors.append(f"module_key = ${len(params)}")
        # always include globals (no route, no module)
        ors.append("(route_pattern is null and module_key is null)")
        where_clauses.append(f"({' or '.join(ors)})")
    params.append(limit)
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            select id, key, route_pattern, module_key, title, body, cta_label, cta_href,
                   priority, category
              from helper_tips
             where {' and '.join(where_clauses)}
             order by priority asc
             limit ${len(params)}
            """, *params,
        )
    return [dict(r) for r in rows]
