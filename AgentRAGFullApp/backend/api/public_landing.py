"""Sprint 27 · Public landing/marketing endpoints (no auth).

Endpoints:
  GET /v1/public/landing-stats         · números agregados para social proof
  GET /v1/public/changelog             · entradas del changelog publicado
  GET /v1/public/changelog/{slug}      · detalle de una entrada
  GET /v1/public/testimonials          · testimonios published (featured first)
  GET /v1/public/plans-bundle          · planes con sus modules+quotas (para /pricing)
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/public", tags=["public-landing"])


@router.get("/landing-stats")
async def landing_stats():
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"firms_total": 0, "matters_total": 0, "documents_total": 0, "citations_total": 0}
    async with storage.pool.acquire() as conn:
        result = await conn.fetchval("select lexai_landing_stats()")
    return result or {}


@router.get("/changelog")
async def public_changelog(
    limit: int = 20,
    highlighted_only: bool = False,
):
    from utils.db import get_storage
    storage = await get_storage()
    where = "published = true"
    if highlighted_only:
        where += " and highlighted = true"
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            select slug, title, summary, category, version,
                   released_at, highlighted
              from changelog_entries
             where {where}
             order by released_at desc
             limit $1
            """, limit,
        )
    return {"items": [dict(r) for r in rows]}


@router.get("/changelog/{slug}")
async def public_changelog_detail(slug: str):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select slug, title, summary, body_md, category, version, released_at
              from changelog_entries
             where slug = $1 and published = true
            """, slug,
        )
    if not row:
        raise HTTPException(404, "changelog entry not found")
    return dict(row)


@router.get("/testimonials")
async def public_testimonials(
    limit: int = 12,
    featured_only: bool = False,
    area: Optional[str] = None,
):
    from utils.db import get_storage
    storage = await get_storage()
    where = ["published = true"]
    params: list = []
    if featured_only:
        where.append("featured = true")
    if area:
        params.append(area)
        where.append(f"area_practica = ${len(params)}")
    params.append(limit)
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            select slug, author_name, author_role, firm_name, firm_logo_url,
                   avatar_url, quote, rating, area_practica, country, featured
              from testimonials
             where {' and '.join(where)}
             order by featured desc, sort_order asc, created_at desc
             limit ${len(params)}
            """, *params,
        )
    return {"items": [dict(r) for r in rows]}


@router.get("/plans-bundle")
async def public_plans_bundle():
    """Planes con sus modules+quotas resueltos · para /pricing público.

    Devuelve mismo formato que /v1/admin/matrix/modules + matrix/quotas
    pero filtrando solo módulos no-admin_only.
    """
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        plans = await conn.fetch(
            """
            select code, name, monthly_cop, annual_cop
              from subscription_plans
             order by monthly_cop asc
            """
        )
        modules = await conn.fetch(
            """
            select key, name, description, category, is_core, sort_order
              from modules
             where category != 'admin_only'
             order by category, sort_order, key
            """
        )
        plan_modules = await conn.fetch(
            "select plan_code, module_key, enabled from plan_modules where enabled = true",
        )
        quota_types = await conn.fetch(
            "select key, name, unit, reset_period, sort_order from quota_types order by sort_order",
        )
        plan_quotas = await conn.fetch(
            "select plan_code, quota_type_key, limit_value, soft_cap_pct from plan_quotas",
        )

    # Build matrix
    mod_map = {}
    for c in plan_modules:
        mod_map[(c["plan_code"], c["module_key"])] = c["enabled"]
    q_map = {}
    for c in plan_quotas:
        q_map[(c["plan_code"], c["quota_type_key"])] = {
            "limit_value": c["limit_value"],
            "soft_cap_pct": c["soft_cap_pct"],
        }

    return {
        "plans": [dict(p) for p in plans],
        "modules": [
            {
                **dict(m),
                "by_plan": {p["code"]: mod_map.get((p["code"], m["key"]), False) for p in plans},
            }
            for m in modules
        ],
        "quotas": [
            {
                **dict(q),
                "by_plan": {
                    p["code"]: q_map.get((p["code"], q["key"]), {"limit_value": 0, "soft_cap_pct": 80})
                    for p in plans
                },
            }
            for q in quota_types
        ],
    }
