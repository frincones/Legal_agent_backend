"""F2 · Judicial notifications API.

Endpoints (REST):
  POST /v1/notifications/judicial/subscribe
  GET  /v1/notifications/judicial?status=unread&matter_id=...
  GET  /v1/notifications/judicial/counts
  PATCH /v1/notifications/judicial/{id}    (mark read/archived)
  POST /v1/notifications/judicial/poll      (force poll all active subs)

The poll endpoint is the entry point Railway's cron will hit (or a
voice tool from the agent).
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/notifications", tags=["notifications"])


class SubscribeRequest(BaseModel):
    matter_id: Optional[str] = None
    fuente: str = Field(default="rama_judicial_demo")
    expediente: str = Field(min_length=2)
    juzgado: Optional[str] = None
    ciudad: Optional[str] = None
    query_extra: dict = Field(default_factory=dict)


@router.post("/judicial/subscribe")
async def subscribe_judicial(
    body: SubscribeRequest,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into judicial_subscriptions
              (firm_id, matter_id, fuente, expediente, juzgado, ciudad, query_extra)
            values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7::jsonb)
            on conflict (firm_id, fuente, expediente)
              do update set juzgado = excluded.juzgado, active = true,
                            updated_at = now()
            returning id
            """,
            principal.firm_id,
            body.matter_id,
            body.fuente,
            body.expediente,
            body.juzgado,
            body.ciudad,
            __import__("json").dumps(body.query_extra),
        )
    return {"id": str(row["id"]), "fuente": body.fuente, "expediente": body.expediente}


@router.get("/judicial")
async def list_judicial(
    status: Optional[str] = Query(default=None, regex="^(unread|read|archived|snoozed)$"),
    severidad: Optional[str] = Query(default=None, regex="^(info|alta|critica)$"),
    matter_id: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    where = ["firm_id = $1::uuid"]
    params: list = [principal.firm_id]
    if status:
        params.append(status)
        where.append(f"status = ${len(params)}")
    if severidad:
        params.append(severidad)
        where.append(f"severidad = ${len(params)}")
    if matter_id:
        params.append(matter_id)
        where.append(f"matter_id = ${len(params)}::uuid")
    params.append(limit)
    sql = f"""
        select id, matter_id, fuente, titulo, resumen, url_oficial,
               fecha_publicacion, fecha_actuacion, expediente, juzgado,
               tipo, severidad, status, created_at, read_at
        from judicial_notifications
        where {' and '.join(where)}
        order by case severidad when 'critica' then 1 when 'alta' then 2 else 3 end,
                 created_at desc
        limit ${len(params)}
    """
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]),
                "matter_id": str(r["matter_id"]) if r["matter_id"] else None,
                "fuente": r["fuente"],
                "titulo": r["titulo"],
                "resumen": r["resumen"],
                "url_oficial": r["url_oficial"],
                "fecha_publicacion": r["fecha_publicacion"].isoformat() if r["fecha_publicacion"] else None,
                "fecha_actuacion": r["fecha_actuacion"].isoformat() if r["fecha_actuacion"] else None,
                "expediente": r["expediente"],
                "juzgado": r["juzgado"],
                "tipo": r["tipo"],
                "severidad": r["severidad"],
                "status": r["status"],
                "created_at": r["created_at"].isoformat(),
                "read_at": r["read_at"].isoformat() if r["read_at"] else None,
            }
            for r in rows
        ],
    }


@router.get("/judicial/counts")
async def judicial_counts(
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        result = await conn.fetchval(
            "select lexai_judicial_counts($1::uuid)", principal.firm_id,
        )
    return result


class PatchRequest(BaseModel):
    status: str = Field(pattern="^(unread|read|archived|snoozed)$")


@router.patch("/judicial/{notification_id}")
async def update_judicial(
    notification_id: str,
    body: PatchRequest,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update judicial_notifications
               set status = $3,
                   read_at = case when $3 = 'read' then now() else read_at end
             where id = $1::uuid and firm_id = $2::uuid
            returning id, status
            """,
            notification_id, principal.firm_id, body.status,
        )
    if not row:
        raise HTTPException(404, "not found")
    return {"id": str(row["id"]), "status": row["status"]}


@router.post("/judicial/poll")
async def poll_now(
    principal: Principal = Depends(get_current_firm),
):
    """Force-poll all active subscriptions of the firm. On-demand entry point."""
    from agent.workers.judicial_poller import poll_firm
    return await poll_firm(principal.firm_id)


# ════════════════════════════════════════════════════════════════════════
# Tools para el agente de voz
# ════════════════════════════════════════════════════════════════════════


async def subscribe_to_expediente_tool(args: dict, ctx: dict) -> dict:
    """Voice tool: 'LexAI, suscríbete a este expediente'."""
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    expediente = (args.get("expediente") or "").strip()
    fuente = args.get("fuente") or "rama_judicial_demo"
    if not (firm_id and expediente):
        return {"error": "expediente requerido"}
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into judicial_subscriptions
              (firm_id, matter_id, fuente, expediente, juzgado, ciudad)
            values ($1::uuid, $2::uuid, $3, $4, $5, $6)
            on conflict (firm_id, fuente, expediente)
              do update set active = true, updated_at = now()
            returning id
            """,
            firm_id, matter_id, fuente, expediente,
            args.get("juzgado"), args.get("ciudad"),
        )
    return {
        "id": str(row["id"]),
        "fuente": fuente,
        "expediente": expediente,
        "matter_id": matter_id,
    }


async def list_judicial_notifications_tool(args: dict, ctx: dict) -> dict:
    """Voice tool: 'LexAI, qué novedades hay en mis casos'."""
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}
    limit = max(1, min(int(args.get("limit") or 10), 25))
    only_unread = bool(args.get("only_unread", True))
    async with storage.pool.acquire() as conn:
        if only_unread:
            rows = await conn.fetch(
                """
                select id, titulo, severidad, fecha_publicacion, expediente,
                       juzgado, tipo, matter_id
                from judicial_notifications
                where firm_id = $1::uuid and status = 'unread'
                order by case severidad when 'critica' then 1 when 'alta' then 2 else 3 end,
                         created_at desc
                limit $2
                """,
                firm_id, limit,
            )
        else:
            rows = await conn.fetch(
                """
                select id, titulo, severidad, fecha_publicacion, expediente,
                       juzgado, tipo, matter_id
                from judicial_notifications
                where firm_id = $1::uuid
                order by created_at desc
                limit $2
                """,
                firm_id, limit,
            )
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]),
                "titulo": r["titulo"],
                "severidad": r["severidad"],
                "fecha_publicacion": r["fecha_publicacion"].isoformat() if r["fecha_publicacion"] else None,
                "expediente": r["expediente"],
                "juzgado": r["juzgado"],
                "tipo": r["tipo"],
                "matter_id": str(r["matter_id"]) if r["matter_id"] else None,
            }
            for r in rows
        ],
    }


async def poll_judicial_now_tool(args: dict, ctx: dict) -> dict:
    """Voice tool: 'LexAI, revisa novedades en mis expedientes ahora'."""
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    from agent.workers.judicial_poller import poll_firm
    result = await poll_firm(firm_id)
    return result
