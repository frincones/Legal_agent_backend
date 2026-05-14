"""Sprint 16 · Mentions inbox API.

  GET    /v1/mentions/inbox?status=unread|read|all&limit=
  GET    /v1/mentions/unread-count
  POST   /v1/mentions/{id}/read
  POST   /v1/mentions/read-all
  POST   /v1/mentions/{id}/unread
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/mentions", tags=["mentions"])


def _serialize(r) -> dict:
    return {
        "id": str(r["id"]),
        "comment_id": str(r["comment_id"]),
        "matter_id": str(r["matter_id"]) if r["matter_id"] else None,
        "matter_document_id": str(r["matter_document_id"]) if r["matter_document_id"] else None,
        "body_preview": r["body_preview"],
        "mentioned_by": str(r["mentioned_by"]) if r["mentioned_by"] else None,
        "mentioned_by_name": r["mentioned_by_name"] if "mentioned_by_name" in r.keys() else None,
        "matter_title": r["matter_title"] if "matter_title" in r.keys() else None,
        "read_at": r["read_at"].isoformat() if r["read_at"] else None,
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


@router.get("/inbox")
async def inbox(
    status: str = Query(default="unread", pattern="^(unread|read|all)$"),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    where = ["m.firm_id = $1::uuid", "m.user_id = $2::uuid"]
    args: list = [principal.firm_id, principal.user_id]
    idx = 3
    if status == "unread":
        where.append("m.read_at is null")
    elif status == "read":
        where.append("m.read_at is not null")
    args.append(limit)
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            select m.id, m.comment_id, m.matter_id, m.matter_document_id,
                   m.body_preview, m.mentioned_by, m.read_at, m.created_at,
                   u.full_name as mentioned_by_name,
                   mat.titulo as matter_title
              from mention_notifications m
              left join users u on u.id = m.mentioned_by
              left join matters mat on mat.id = m.matter_id
             where {' and '.join(where)}
             order by m.created_at desc
             limit ${idx}
            """,
            *args,
        )
    return {"items": [_serialize(r) for r in rows], "count": len(rows)}


@router.get("/unread-count")
async def unread_count(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"count": 0}
    async with storage.pool.acquire() as conn:
        count = await conn.fetchval(
            "select lexai_unread_mentions($1::uuid, $2::uuid)",
            principal.firm_id, principal.user_id,
        )
    return {"count": int(count or 0)}


@router.post("/{mention_id}/read")
async def mark_read(
    mention_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update mention_notifications
               set read_at = now()
             where firm_id = $1::uuid and user_id = $2::uuid and id = $3::uuid
             returning id
            """,
            principal.firm_id, principal.user_id, mention_id,
        )
    if not row:
        raise HTTPException(404, "Mención no encontrada")
    return {"ok": True, "id": str(row["id"])}


@router.post("/{mention_id}/unread")
async def mark_unread(
    mention_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            update mention_notifications
               set read_at = null
             where firm_id = $1::uuid and user_id = $2::uuid and id = $3::uuid
            """,
            principal.firm_id, principal.user_id, mention_id,
        )
    return {"ok": True}


@router.post("/read-all")
async def mark_all_read(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        result = await conn.execute(
            """
            update mention_notifications
               set read_at = now()
             where firm_id = $1::uuid and user_id = $2::uuid and read_at is null
            """,
            principal.firm_id, principal.user_id,
        )
    return {"ok": True, "result": result}
