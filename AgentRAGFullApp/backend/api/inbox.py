"""Sprint 5 · Inbox unificado.

Vista única que mezcla:
  - judicial_notifications (Court Watcher)
  - email_messages         (Gmail/Outlook · is_legal=true)
  - legal_alerts           (alertas internas del sistema)

GET /v1/inbox/unified?status=unread&limit=50
GET /v1/inbox/counts
PATCH /v1/inbox/{kind}/{id}    {status: 'read' | 'archived' | 'snoozed'}

Cada item se devuelve con un campo discriminador `kind` para que el
frontend renderice el ícono y la acción correctos.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/inbox", tags=["inbox"])


@router.get("/unified")
async def unified_feed(
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

    # Mapeo severidad: legal_alerts usa 'info|warning|critical', otros usan 'info|alta|critica'.
    sev_map_alert = {"info": "info", "alta": "warning", "critica": "critical"}
    where_jud: list[str] = ["firm_id = $1::uuid"]
    where_email: list[str] = ["firm_id = $1::uuid", "is_legal = true"]
    where_alert: list[str] = ["firm_id = $1::uuid", "dismissed_at is null"]
    params: list = [principal.firm_id]

    if status:
        params.append(status)
        idx = len(params)
        where_jud.append(f"status = ${idx}")
        where_email.append(f"status = ${idx}")
        # legal_alerts: 'unread' = read_at is null, 'read' = read_at is not null
        if status == "unread":
            where_alert.append("read_at is null")
        elif status == "read":
            where_alert.append("read_at is not null")
        elif status == "archived":
            where_alert.append("dismissed_at is not null")
            # ya excluido arriba; re-permitir
            where_alert.remove("dismissed_at is null")
    if severidad:
        params.append(severidad)
        idx = len(params)
        where_jud.append(f"severidad = ${idx}")
        where_email.append(f"severidad = ${idx}")
        # alert.severity tiene mapping distinto; usamos literal mapeado
        mapped = sev_map_alert.get(severidad)
        if mapped:
            where_alert.append(f"severity = '{mapped}'")
        else:
            where_alert.append("false")
    if matter_id:
        params.append(matter_id)
        idx = len(params)
        where_jud.append(f"matter_id = ${idx}::uuid")
        where_email.append(f"matter_id = ${idx}::uuid")
        where_alert.append(f"${idx}::uuid = any(affected_matter_ids)")

    params.append(limit)
    limit_param = f"${len(params)}"

    sql = f"""
        with jud as (
          select 'judicial'::text as kind, id, matter_id, titulo as title,
                 resumen as snippet, severidad, status, created_at,
                 expediente as ref, juzgado as source, url_oficial as url,
                 fecha_publicacion as event_date
            from judicial_notifications
            where {' and '.join(where_jud)}
        ), email as (
          select 'email'::text as kind, id, matter_id, subject as title,
                 coalesce(parsed_summary, snippet) as snippet,
                 severidad, status, received_at as created_at,
                 matched_expediente as ref, from_address as source,
                 null::text as url, matched_fecha as event_date
            from email_messages
            where {' and '.join(where_email)}
        ), alert as (
          select 'alert'::text as kind, id, null::uuid as matter_id, title,
                 description as snippet,
                 case severity when 'critical' then 'critica'
                               when 'warning' then 'alta'
                               else 'info' end as severidad,
                 case when read_at is not null then 'read'
                      when dismissed_at is not null then 'archived'
                      else 'unread' end as status,
                 detected_at as created_at,
                 target_ref as ref, source, source_url as url,
                 null::date as event_date
            from legal_alerts
            where {' and '.join(where_alert)}
        )
        select * from jud
        union all select * from email
        union all select * from alert
        order by case severidad when 'critica' then 1 when 'alta' then 2 else 3 end,
                 created_at desc
        limit {limit_param}
    """

    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {
        "count": len(rows),
        "items": [
            {
                "kind": r["kind"],
                "id": str(r["id"]),
                "matter_id": str(r["matter_id"]) if r["matter_id"] else None,
                "title": r["title"],
                "snippet": r["snippet"],
                "severidad": r["severidad"],
                "status": r["status"],
                "ref": r["ref"],
                "source": r["source"],
                "url": r["url"],
                "event_date": r["event_date"].isoformat() if r["event_date"] else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


@router.get("/counts")
async def inbox_counts(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select
              (select count(*) from judicial_notifications
                 where firm_id = $1::uuid and status = 'unread') as judicial_unread,
              (select count(*) from email_messages
                 where firm_id = $1::uuid and is_legal = true and status = 'unread') as email_unread,
              (select count(*) from legal_alerts
                 where firm_id = $1::uuid and read_at is null and dismissed_at is null) as alerts_unread,
              (select count(*) from judicial_notifications
                 where firm_id = $1::uuid and status = 'unread' and severidad = 'critica') as judicial_critical,
              (select count(*) from email_messages
                 where firm_id = $1::uuid and is_legal = true and status = 'unread' and severidad = 'critica') as email_critical
            """,
            principal.firm_id,
        )
    total = (row["judicial_unread"] or 0) + (row["email_unread"] or 0) + (row["alerts_unread"] or 0)
    critical = (row["judicial_critical"] or 0) + (row["email_critical"] or 0)
    return {
        "judicial_unread": row["judicial_unread"] or 0,
        "email_unread": row["email_unread"] or 0,
        "alerts_unread": row["alerts_unread"] or 0,
        "total_unread": total,
        "critical": critical,
    }


class PatchStatusRequest(BaseModel):
    status: str = Field(pattern="^(unread|read|archived|snoozed)$")


@router.patch("/{kind}/{item_id}")
async def patch_inbox_item(
    kind: str,
    item_id: str,
    body: PatchStatusRequest,
    principal: Principal = Depends(get_current_firm),
):
    if kind not in ("judicial", "email", "alert"):
        raise HTTPException(400, "kind invalido")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    if kind == "alert":
        # legal_alerts no tiene columna `status`; usamos read_at + dismissed_at.
        if body.status == "read":
            sql = (
                "update legal_alerts set read_at = now() "
                "where id = $1::uuid and firm_id = $2::uuid returning id"
            )
        elif body.status == "archived":
            sql = (
                "update legal_alerts set dismissed_at = now() "
                "where id = $1::uuid and firm_id = $2::uuid returning id"
            )
        elif body.status == "unread":
            sql = (
                "update legal_alerts set read_at = null, dismissed_at = null "
                "where id = $1::uuid and firm_id = $2::uuid returning id"
            )
        else:
            sql = (
                "update legal_alerts set read_at = now() "
                "where id = $1::uuid and firm_id = $2::uuid returning id"
            )
        async with storage.pool.acquire() as conn:
            row = await conn.fetchrow(sql, item_id, principal.firm_id)
        if not row:
            raise HTTPException(404, "not found")
        return {"id": str(row["id"]), "kind": kind, "status": body.status}

    table = {"judicial": "judicial_notifications", "email": "email_messages"}[kind]
    sql = f"""
        update {table} set status = $3,
               read_at = case when $3 = 'read' then now() else read_at end
         where id = $1::uuid and firm_id = $2::uuid
        returning id, status
    """
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(sql, item_id, principal.firm_id, body.status)
    if not row:
        raise HTTPException(404, "not found")
    return {"id": str(row["id"]), "kind": kind, "status": row["status"]}
