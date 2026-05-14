"""Sprint 16 · Comments API.

Endpoints:
  GET    /v1/comments?matter_id= | ?matter_document_id= | ?thread_root_id=
  GET    /v1/comments/{id}
  POST   /v1/comments
  PATCH  /v1/comments/{id}       (solo body · author-only o admin)
  POST   /v1/comments/{id}/resolve
  POST   /v1/comments/{id}/unresolve
  POST   /v1/comments/{id}/reply
  DELETE /v1/comments/{id}       (author-only o admin)

Hilos:
  thread_root_id se calcula vía trigger SQL · cliente sólo manda parent_id.

Menciones:
  Auto-parseadas del body (utils.mentions) y persistidas en `mentions uuid[]`.
  Los triggers SQL pueblan mention_notifications + activity_events.

Push:
  Después del insert, hacemos best-effort push a los usuarios mencionados
  vía utils.push (reuso Sprint 12). Si push está deshabilitado, no falla.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/comments", tags=["comments"])


VALID_ANCHORS = {"matter", "matter_document", "canvas", "lesson", "kb_entry"}


# --------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------
class CommentIn(BaseModel):
    anchor_kind: str
    body: str = Field(..., min_length=1, max_length=8000)
    matter_id: Optional[str] = None
    matter_document_id: Optional[str] = None
    lesson_id: Optional[str] = None
    kb_entry_id: Optional[str] = None
    anchor_ref: dict = Field(default_factory=dict)
    parent_id: Optional[str] = None


class CommentReply(BaseModel):
    body: str = Field(..., min_length=1, max_length=8000)


class CommentPatch(BaseModel):
    body: str = Field(..., min_length=1, max_length=8000)


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------
def _serialize(r) -> dict:
    keys = set(r.keys()) if hasattr(r, "keys") else set()
    def _opt(k: str):
        return r[k] if k in keys else None
    return {
        "id": str(r["id"]),
        "anchor_kind": r["anchor_kind"],
        "matter_id": str(r["matter_id"]) if r["matter_id"] else None,
        "matter_document_id": str(r["matter_document_id"]) if r["matter_document_id"] else None,
        "lesson_id": str(r["lesson_id"]) if r["lesson_id"] else None,
        "kb_entry_id": str(r["kb_entry_id"]) if r["kb_entry_id"] else None,
        "anchor_ref": r["anchor_ref"] if not isinstance(r["anchor_ref"], str) else (json.loads(r["anchor_ref"]) if r["anchor_ref"] else {}),
        "parent_id": str(r["parent_id"]) if r["parent_id"] else None,
        "thread_root_id": str(r["thread_root_id"]) if r["thread_root_id"] else None,
        "body": r["body"],
        "mentions": [str(m) for m in (r["mentions"] or [])],
        "resolved": bool(r["resolved"]),
        "resolved_by": str(r["resolved_by"]) if r["resolved_by"] else None,
        "resolved_at": r["resolved_at"].isoformat() if r["resolved_at"] else None,
        "edited_at": r["edited_at"].isoformat() if r["edited_at"] else None,
        "created_by": str(r["created_by"]) if r["created_by"] else None,
        "author_name": _opt("author_name"),
        "author_avatar": _opt("author_avatar"),
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


async def _push_to_mentioned(firm_id: str, user_ids: list[str], comment_id: str,
                              actor_name: str, preview: str, matter_id: Optional[str]) -> None:
    """Dispara push best-effort a usuarios mencionados via Sprint 12 dispatch."""
    if not user_ids:
        return
    try:
        from api.push import dispatch_to_user
    except Exception as e:
        logger.debug("push dispatch import failed: %s", e)
        return
    title = f"{actor_name} te mencionó"
    body_preview = preview[:160]
    url = f"/casos/{matter_id}?comment={comment_id}" if matter_id else f"/actividad?comment={comment_id}"
    for uid in user_ids:
        try:
            await dispatch_to_user(
                user_id=uid, firm_id=firm_id,
                title=title, body=body_preview, url=url,
            )
        except Exception as e:
            logger.debug("dispatch_to_user failed (uid=%s): %s", uid, e)


# --------------------------------------------------------------------
# List
# --------------------------------------------------------------------
@router.get("")
async def list_comments(
    matter_id: Optional[str] = Query(default=None),
    matter_document_id: Optional[str] = Query(default=None),
    thread_root_id: Optional[str] = Query(default=None),
    lesson_id: Optional[str] = Query(default=None),
    kb_entry_id: Optional[str] = Query(default=None),
    include_resolved: bool = Query(default=True),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_current_firm),
):
    if not any([matter_id, matter_document_id, thread_root_id, lesson_id, kb_entry_id]):
        raise HTTPException(400, "Indica al menos un filtro (matter_id, matter_document_id, thread_root_id, lesson_id, kb_entry_id)")
    where = ["c.firm_id = $1::uuid"]
    args: list = [principal.firm_id]
    idx = 2
    if matter_id:
        where.append(f"c.matter_id = ${idx}::uuid"); args.append(matter_id); idx += 1
    if matter_document_id:
        where.append(f"c.matter_document_id = ${idx}::uuid"); args.append(matter_document_id); idx += 1
    if thread_root_id:
        where.append(f"c.thread_root_id = ${idx}::uuid"); args.append(thread_root_id); idx += 1
    if lesson_id:
        where.append(f"c.lesson_id = ${idx}::uuid"); args.append(lesson_id); idx += 1
    if kb_entry_id:
        where.append(f"c.kb_entry_id = ${idx}::uuid"); args.append(kb_entry_id); idx += 1
    if not include_resolved:
        where.append("c.resolved = false")
    args.append(limit)
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            select c.id, c.anchor_kind, c.matter_id, c.matter_document_id,
                   c.lesson_id, c.kb_entry_id, c.anchor_ref, c.parent_id,
                   c.thread_root_id, c.body, c.mentions, c.resolved, c.resolved_by,
                   c.resolved_at, c.edited_at, c.created_by, c.created_at, c.updated_at,
                   u.full_name as author_name, u.avatar_url as author_avatar
              from comments c
              left join users u on u.id = c.created_by
             where {' and '.join(where)}
             order by c.created_at asc
             limit ${idx}
            """,
            *args,
        )
    return {"items": [_serialize(r) for r in rows], "count": len(rows)}


# --------------------------------------------------------------------
# Get
# --------------------------------------------------------------------
@router.get("/{comment_id}")
async def get_comment(
    comment_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select c.*, u.full_name as author_name, u.avatar_url as author_avatar
              from comments c
              left join users u on u.id = c.created_by
             where c.firm_id = $1::uuid and c.id = $2::uuid
            """,
            principal.firm_id, comment_id,
        )
    if not row:
        raise HTTPException(404, "Comentario no encontrado")
    return _serialize(row)


# --------------------------------------------------------------------
# Create
# --------------------------------------------------------------------
@router.post("", status_code=201)
async def create_comment(
    body: CommentIn,
    principal: Principal = Depends(get_current_firm),
):
    if body.anchor_kind not in VALID_ANCHORS:
        raise HTTPException(400, f"anchor_kind inválido (válidos: {sorted(VALID_ANCHORS)})")

    # Validación mínima de anchors
    if body.anchor_kind == "matter" and not body.matter_id:
        raise HTTPException(400, "matter_id requerido para anchor_kind='matter'")
    if body.anchor_kind == "matter_document" and not body.matter_document_id:
        raise HTTPException(400, "matter_document_id requerido para anchor_kind='matter_document'")
    if body.anchor_kind == "lesson" and not body.lesson_id:
        raise HTTPException(400, "lesson_id requerido para anchor_kind='lesson'")
    if body.anchor_kind == "kb_entry" and not body.kb_entry_id:
        raise HTTPException(400, "kb_entry_id requerido para anchor_kind='kb_entry'")
    if body.anchor_kind == "canvas" and not body.matter_id:
        raise HTTPException(400, "matter_id requerido para anchor_kind='canvas'")

    from utils.db import get_storage
    from utils.mentions import resolve_mentions, render_preview
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")

    async with storage.pool.acquire() as conn:
        # Parseo de menciones contra los users de la firm
        mention_user_ids = await resolve_mentions(conn, principal.firm_id, body.body)
        row = await conn.fetchrow(
            """
            insert into comments
              (firm_id, anchor_kind, matter_id, matter_document_id, lesson_id, kb_entry_id,
               anchor_ref, parent_id, body, mentions, created_by)
            values ($1::uuid, $2, $3::uuid, $4::uuid, $5::uuid, $6::uuid,
                    $7::jsonb, $8::uuid, $9, $10::uuid[], $11::uuid)
            returning *, (
              select full_name from users where id = $11::uuid
            ) as author_name,
            (select avatar_url from users where id = $11::uuid) as author_avatar
            """,
            principal.firm_id, body.anchor_kind,
            body.matter_id, body.matter_document_id, body.lesson_id, body.kb_entry_id,
            json.dumps(body.anchor_ref or {}),
            body.parent_id,
            body.body,
            mention_user_ids,
            principal.user_id,
        )

    serialized = _serialize(row)
    # Push best-effort
    if mention_user_ids:
        await _push_to_mentioned(
            firm_id=principal.firm_id,
            user_ids=mention_user_ids,
            comment_id=serialized["id"],
            actor_name=row["author_name"] or "Alguien",
            preview=render_preview(body.body),
            matter_id=body.matter_id,
        )
    return serialized


# --------------------------------------------------------------------
# Reply (shortcut)
# --------------------------------------------------------------------
@router.post("/{comment_id}/reply", status_code=201)
async def reply_comment(
    comment_id: str,
    body: CommentReply,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        parent = await conn.fetchrow(
            """
            select anchor_kind, matter_id, matter_document_id, lesson_id, kb_entry_id, anchor_ref
              from comments
             where firm_id = $1::uuid and id = $2::uuid
            """,
            principal.firm_id, comment_id,
        )
    if not parent:
        raise HTTPException(404, "Comentario padre no encontrado")
    payload = CommentIn(
        anchor_kind=parent["anchor_kind"],
        matter_id=str(parent["matter_id"]) if parent["matter_id"] else None,
        matter_document_id=str(parent["matter_document_id"]) if parent["matter_document_id"] else None,
        lesson_id=str(parent["lesson_id"]) if parent["lesson_id"] else None,
        kb_entry_id=str(parent["kb_entry_id"]) if parent["kb_entry_id"] else None,
        anchor_ref=parent["anchor_ref"] if not isinstance(parent["anchor_ref"], str) else (json.loads(parent["anchor_ref"]) if parent["anchor_ref"] else {}),
        parent_id=comment_id,
        body=body.body,
    )
    return await create_comment(payload, principal)  # type: ignore[arg-type]


# --------------------------------------------------------------------
# Edit
# --------------------------------------------------------------------
@router.patch("/{comment_id}")
async def edit_comment(
    comment_id: str,
    body: CommentPatch,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    from utils.mentions import resolve_mentions
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        current = await conn.fetchrow(
            "select created_by from comments where firm_id = $1::uuid and id = $2::uuid",
            principal.firm_id, comment_id,
        )
        if not current:
            raise HTTPException(404, "Comentario no encontrado")
        is_owner = str(current["created_by"]) == str(principal.user_id)
        is_admin = principal.role in ("admin", "socio_senior")
        if not (is_owner or is_admin):
            raise HTTPException(403, "Solo el autor o admin puede editar")
        mention_user_ids = await resolve_mentions(conn, principal.firm_id, body.body)
        row = await conn.fetchrow(
            """
            update comments
               set body = $1, mentions = $2::uuid[], edited_at = now()
             where firm_id = $3::uuid and id = $4::uuid
             returning *, (
               select full_name from users where id = comments.created_by
             ) as author_name,
             (select avatar_url from users where id = comments.created_by) as author_avatar
            """,
            body.body, mention_user_ids, principal.firm_id, comment_id,
        )
    return _serialize(row)


# --------------------------------------------------------------------
# Resolve / Unresolve
# --------------------------------------------------------------------
@router.post("/{comment_id}/resolve")
async def resolve_comment(
    comment_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update comments
               set resolved = true, resolved_by = $1::uuid, resolved_at = now()
             where firm_id = $2::uuid and id = $3::uuid
             returning id
            """,
            principal.user_id, principal.firm_id, comment_id,
        )
    if not row:
        raise HTTPException(404, "Comentario no encontrado")
    return {"resolved": True, "id": str(row["id"])}


@router.post("/{comment_id}/unresolve")
async def unresolve_comment(
    comment_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "update comments set resolved = false, resolved_by = null, resolved_at = null "
            "where firm_id = $1::uuid and id = $2::uuid",
            principal.firm_id, comment_id,
        )
    return {"resolved": False}


# --------------------------------------------------------------------
# Delete
# --------------------------------------------------------------------
@router.delete("/{comment_id}")
async def delete_comment(
    comment_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        current = await conn.fetchrow(
            "select created_by from comments where firm_id = $1::uuid and id = $2::uuid",
            principal.firm_id, comment_id,
        )
        if not current:
            raise HTTPException(404, "Comentario no encontrado")
        is_owner = str(current["created_by"]) == str(principal.user_id)
        is_admin = principal.role in ("admin", "socio_senior")
        if not (is_owner or is_admin):
            raise HTTPException(403, "Solo el autor o admin puede borrar")
        await conn.execute(
            "delete from comments where firm_id = $1::uuid and id = $2::uuid",
            principal.firm_id, comment_id,
        )
    return {"deleted": True}
