"""Sprint 7 · Analytics API · KPIs y reportes para socios.

Endpoints:
  GET /v1/analytics/kpis?days=30        · overview de la firma (RPC lexai_firm_kpis)
  GET /v1/analytics/lawyers?days=30     · performance por abogado responsable
  GET /v1/analytics/deadlines?days=30   · plazos cumplidos/incumplidos
  GET /v1/analytics/citations?days=30   · citas verified/outdated por mes
  GET /v1/analytics/timeline?days=30    · serie temporal (matters/docs/deadlines)
  POST /v1/analytics/refresh-cache      · admin · regenera firm_reports

Permisos:
  · Lectura: cualquier usuario autenticado de la firma
  · Refresh: admin / socio_senior
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/analytics", tags=["analytics"])


@router.get("/kpis")
async def kpis(
    days: int = Query(default=30, le=365),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        result = await conn.fetchval(
            "select lexai_firm_kpis($1::uuid, $2::int)", principal.firm_id, days
        )
    return result or {}


@router.get("/lawyers")
async def lawyers_performance(
    days: int = Query(default=30, le=365),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            with d as (select (now() - ($2::text || ' days')::interval) as ts)
            select u.id, u.full_name, u.role,
              (select count(*) from matters m
                 where m.firm_id = $1::uuid and m.owner_user_id = u.id) as matters_total,
              (select count(*) from matters m
                 where m.firm_id = $1::uuid and m.owner_user_id = u.id
                   and m.status = 'activo') as matters_active,
              (select count(*) from matters m
                 where m.firm_id = $1::uuid and m.owner_user_id = u.id
                   and m.created_at >= (select ts from d)) as matters_new,
              (select count(*) from matter_deadlines md
                 join matters m2 on m2.id = md.matter_id
                 where m2.owner_user_id = u.id and md.completado = true
                   and md.fecha >= (select ts from d)::date) as deadlines_done,
              (select count(*) from matter_deadlines md
                 join matters m2 on m2.id = md.matter_id
                 where m2.owner_user_id = u.id and md.completado = false
                   and md.fecha < current_date) as deadlines_overdue
              from users u
             where u.firm_id = $1::uuid
             order by matters_active desc, u.full_name
             limit 100
            """,
            principal.firm_id, days,
        )
    return {
        "count": len(rows),
        "items": [
            {
                "user_id": str(r["id"]),
                "full_name": r["full_name"],
                "role": r["role"],
                "matters_total": r["matters_total"],
                "matters_active": r["matters_active"],
                "matters_new": r["matters_new"],
                "deadlines_done": r["deadlines_done"],
                "deadlines_overdue": r["deadlines_overdue"],
            }
            for r in rows
        ],
    }


@router.get("/deadlines")
async def deadlines_stats(
    days: int = Query(default=30, le=365),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            with d as (select (current_date - $2::int) as since)
            select date_trunc('day', fecha)::date as day,
                   count(*) filter (where completado = true) as done,
                   count(*) filter (where completado = false and fecha < current_date) as overdue,
                   count(*) filter (where completado = false and fecha >= current_date) as upcoming
              from matter_deadlines
             where firm_id = $1::uuid
               and fecha >= (select since from d)
             group by day
             order by day asc
            """,
            principal.firm_id, days,
        )
    return {
        "count": len(rows),
        "items": [
            {
                "day": r["day"].isoformat() if r["day"] else None,
                "done": r["done"],
                "overdue": r["overdue"],
                "upcoming": r["upcoming"],
            }
            for r in rows
        ],
    }


@router.get("/citations")
async def citations_stats(
    days: int = Query(default=90, le=365),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    # document_citations tiene estatus 'verificada','superada','sospechosa','no_encontrada'
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select date_trunc('week', dc.created_at)::date as week,
                   status, count(*) as n
              from document_citations dc
              join matter_documents md on md.id = dc.matter_document_id
             where md.firm_id = $1::uuid
               and dc.created_at >= now() - ($2::text || ' days')::interval
             group by week, status
             order by week asc
            """,
            principal.firm_id, days,
        )
    return {
        "count": len(rows),
        "items": [
            {
                "week": r["week"].isoformat() if r["week"] else None,
                "status": r["status"],
                "count": r["n"],
            }
            for r in rows
        ],
    }


@router.get("/timeline")
async def timeline_stats(
    days: int = Query(default=30, le=365),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            with d as (select generate_series(
                          date_trunc('day', now()) - ($2::text || ' days')::interval,
                          date_trunc('day', now()), '1 day'::interval)::date as day)
            select d.day,
              (select count(*) from matters m
                 where m.firm_id = $1::uuid and date_trunc('day', m.created_at)::date = d.day) as matters_created,
              (select count(*) from matter_documents md
                 where md.firm_id = $1::uuid and date_trunc('day', md.created_at)::date = d.day) as docs_uploaded,
              (select count(*) from matter_deadlines mdl
                 where mdl.firm_id = $1::uuid and mdl.completado = true and mdl.fecha = d.day) as deadlines_done
            from d
            order by d.day asc
            """,
            principal.firm_id, days,
        )
    return {
        "count": len(rows),
        "items": [
            {
                "day": r["day"].isoformat() if r["day"] else None,
                "matters_created": r["matters_created"],
                "docs_uploaded": r["docs_uploaded"],
                "deadlines_done": r["deadlines_done"],
            }
            for r in rows
        ],
    }


@router.post("/refresh-cache")
async def refresh_cache(principal: Principal = Depends(get_current_firm)):
    if principal.role not in ("admin", "socio_senior", "socio_junior"):
        raise HTTPException(403, "Solo socios/admin")
    from agent.workers.report_builder import refresh_firm_reports
    return await refresh_firm_reports(str(principal.firm_id))


# ════════════════════════════════════════════════════════════════════════
# Voice tool
# ════════════════════════════════════════════════════════════════════════


async def analyze_firm_performance_tool(args: dict, ctx: dict) -> dict:
    """Voice tool: 'LexAI, ¿cómo va la firma este mes?'."""
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    days = int(args.get("days") or 30)
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}
    async with storage.pool.acquire() as conn:
        kpis = await conn.fetchval("select lexai_firm_kpis($1::uuid, $2::int)", firm_id, days)
        lawyers = await conn.fetch(
            """
            select u.full_name, count(m.id) as n
              from users u
              left join matters m on m.owner_user_id = u.id and m.status = 'activo'
             where u.firm_id = $1::uuid
             group by u.id, u.full_name
             order by n desc limit 5
            """,
            firm_id,
        )
    return {
        "kpis": kpis or {},
        "top_lawyers": [{"name": r["full_name"], "active_matters": r["n"]} for r in lawyers],
    }
