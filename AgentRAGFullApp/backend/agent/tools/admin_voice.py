"""Sprint 24 · Voice tools de admin SaaS (para el operador del SaaS).

Estos tools sólo deberían exponerse cuando el usuario del agente es un
admin_users.active=true. El registrar enmascara automáticamente · si no
hay admin context, devuelven "no autorizado".

Tools:
  · saas_mrr_now()                · MRR / ARR / ARPU del momento
  · saas_signups_mtd()            · signups en el mes + crecimiento
  · saas_churn_30d()              · churn rolling 30 días
  · search_firm_by_name(q)        · busca firms por nombre (top 5)
  · firm_health_snapshot(firm_id) · resumen completo de una firm
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def _is_admin(user_id: Optional[str]) -> bool:
    if not user_id:
        return False
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return False
    async with storage.pool.acquire() as conn:
        ok = await conn.fetchval(
            "select exists (select 1 from admin_users where auth_user_id = $1::uuid and active = true)",
            user_id,
        )
    return bool(ok)


async def saas_mrr_now_tool(args: dict, ctx: dict) -> dict:
    if not await _is_admin(ctx.get("user_id")):
        return {"error": "Acceso restringido al admin SaaS"}
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        return await conn.fetchval("select lexai_saas_mrr()") or {}


async def saas_signups_mtd_tool(args: dict, ctx: dict) -> dict:
    if not await _is_admin(ctx.get("user_id")):
        return {"error": "Acceso restringido al admin SaaS"}
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        return await conn.fetchval("select lexai_saas_signups_mtd()") or {}


async def saas_churn_30d_tool(args: dict, ctx: dict) -> dict:
    if not await _is_admin(ctx.get("user_id")):
        return {"error": "Acceso restringido al admin SaaS"}
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        return await conn.fetchval("select lexai_saas_churn_30d()") or {}


async def search_firm_by_name_tool(args: dict, ctx: dict) -> dict:
    if not await _is_admin(ctx.get("user_id")):
        return {"error": "Acceso restringido al admin SaaS"}
    q = (args.get("q") or "").strip().lower()
    if not q or len(q) < 2:
        return {"items": []}
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select f.id, f.razon_social, f.country, f.created_at,
                   s.plan_code, s.status
              from firms f
              left join firm_subscriptions s on s.firm_id = f.id
             where lower(f.razon_social) like $1
             order by f.created_at desc
             limit 5
            """,
            f"%{q}%",
        )
    return {"items": [dict(r) for r in rows]}


async def firm_health_snapshot_tool(args: dict, ctx: dict) -> dict:
    if not await _is_admin(ctx.get("user_id")):
        return {"error": "Acceso restringido al admin SaaS"}
    firm_id = args.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        return await conn.fetchval("select lexai_saas_firm_health($1::uuid)", firm_id) or {}
