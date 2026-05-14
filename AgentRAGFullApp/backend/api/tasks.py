"""Sprint 17 · Tasks API.

Tareas asignables ligeras (no es Jira · es un to-do del despacho).

  GET    /v1/tasks?status=&assignee_user_id=&matter_id=&priority=&due_before=&q=
  GET    /v1/tasks/{id}
  POST   /v1/tasks
  PATCH  /v1/tasks/{id}
  POST   /v1/tasks/{id}/complete
  POST   /v1/tasks/{id}/reopen
  DELETE /v1/tasks/{id}
  GET    /v1/tasks/counts                · counts por status/priority/overdue
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/tasks", tags=["tasks"])


VALID_STATUS = {"open", "in_progress", "blocked", "done", "cancelled"}
VALID_PRIORITY = {"low", "normal", "high", "urgent"}


class TaskIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=240)
    description: Optional[str] = None
    matter_id: Optional[str] = None
    assignee_user_id: Optional[str] = None
    priority: str = "normal"
    due_at: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    source_comment_id: Optional[str] = None
    source_lesson_id: Optional[str] = None
    source_document_id: Optional[str] = None


class TaskPatch(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    matter_id: Optional[str] = None
    assignee_user_id: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    due_at: Optional[str] = None
    tags: Optional[list[str]] = None


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(400, f"due_at inválido: {s}")


def _serialize(r) -> dict:
    keys = set(r.keys()) if hasattr(r, "keys") else set()
    def _opt(k):
        return r[k] if k in keys else None
    return {
        "id": str(r["id"]),
        "title": r["title"],
        "description": r["description"],
        "status": r["status"],
        "priority": r["priority"],
        "matter_id": str(r["matter_id"]) if r["matter_id"] else None,
        "matter_titulo": _opt("matter_titulo"),
        "assignee_user_id": str(r["assignee_user_id"]) if r["assignee_user_id"] else None,
        "assignee_name": _opt("assignee_name"),
        "due_at": r["due_at"].isoformat() if r["due_at"] else None,
        "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
        "completed_by": str(r["completed_by"]) if r["completed_by"] else None,
        "source": r["source"],
        "source_comment_id": str(r["source_comment_id"]) if r["source_comment_id"] else None,
        "source_lesson_id": str(r["source_lesson_id"]) if r["source_lesson_id"] else None,
        "source_document_id": str(r["source_document_id"]) if r["source_document_id"] else None,
        "tags": list(r["tags"] or []),
        "created_by": str(r["created_by"]) if r["created_by"] else None,
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


@router.get("")
async def list_tasks(
    status: Optional[str] = Query(default=None),
    assignee_user_id: Optional[str] = Query(default=None),
    matter_id: Optional[str] = Query(default=None),
    priority: Optional[str] = Query(default=None),
    tag: Optional[str] = Query(default=None),
    due_before: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None, max_length=200),
    mine: bool = Query(default=False, description="Solo las asignadas a mí"),
    open_only: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_current_firm),
):
    where = ["t.firm_id = $1::uuid"]
    args: list = [principal.firm_id]
    idx = 2

    if status:
        if status not in VALID_STATUS:
            raise HTTPException(400, f"status inválido (válidos: {sorted(VALID_STATUS)})")
        where.append(f"t.status = ${idx}"); args.append(status); idx += 1
    if open_only:
        where.append("t.status in ('open','in_progress','blocked')")
    if mine:
        where.append(f"t.assignee_user_id = ${idx}::uuid"); args.append(principal.user_id); idx += 1
    elif assignee_user_id:
        where.append(f"t.assignee_user_id = ${idx}::uuid"); args.append(assignee_user_id); idx += 1
    if matter_id:
        where.append(f"t.matter_id = ${idx}::uuid"); args.append(matter_id); idx += 1
    if priority:
        if priority not in VALID_PRIORITY:
            raise HTTPException(400, f"priority inválido (válidos: {sorted(VALID_PRIORITY)})")
        where.append(f"t.priority = ${idx}"); args.append(priority); idx += 1
    if tag:
        where.append(f"${idx} = ANY(t.tags)"); args.append(tag); idx += 1
    if due_before:
        dt = _parse_dt(due_before)
        where.append(f"t.due_at <= ${idx}"); args.append(dt); idx += 1
    if q:
        where.append(f"(t.title ilike ${idx} or coalesce(t.description,'') ilike ${idx})")
        args.append(f"%{q}%"); idx += 1

    args.extend([limit, offset])

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            select t.id, t.title, t.description, t.status, t.priority,
                   t.matter_id, t.assignee_user_id, t.due_at, t.completed_at,
                   t.completed_by, t.source, t.source_comment_id,
                   t.source_lesson_id, t.source_document_id, t.tags,
                   t.created_by, t.created_at, t.updated_at,
                   m.titulo as matter_titulo,
                   u.full_name as assignee_name
              from tasks t
              left join matters m on m.id = t.matter_id
              left join users u on u.id = t.assignee_user_id
             where {' and '.join(where)}
             order by case t.priority
                        when 'urgent' then 1 when 'high' then 2
                        when 'normal' then 3 else 4 end,
                      t.due_at nulls last,
                      t.created_at desc
             limit ${idx} offset ${idx + 1}
            """,
            *args,
        )
    return {"items": [_serialize(r) for r in rows], "count": len(rows)}


@router.get("/counts")
async def counts(principal: Principal = Depends(get_current_firm)):
    """Counts por status + overdue + mine_open."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {}
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select
              count(*) filter (where status = 'open') as open,
              count(*) filter (where status = 'in_progress') as in_progress,
              count(*) filter (where status = 'blocked') as blocked,
              count(*) filter (where status = 'done') as done,
              count(*) filter (where status in ('open','in_progress','blocked')
                              and due_at is not null and due_at < now()) as overdue,
              count(*) filter (where status in ('open','in_progress','blocked')
                              and assignee_user_id = $2::uuid) as mine_open
              from tasks
             where firm_id = $1::uuid
            """,
            principal.firm_id, principal.user_id,
        )
    return {k: int(row[k] or 0) for k in ("open", "in_progress", "blocked", "done", "overdue", "mine_open")}


@router.get("/{task_id}")
async def get_task(
    task_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select t.*, m.titulo as matter_titulo, u.full_name as assignee_name
              from tasks t
              left join matters m on m.id = t.matter_id
              left join users u on u.id = t.assignee_user_id
             where t.firm_id = $1::uuid and t.id = $2::uuid
            """,
            principal.firm_id, task_id,
        )
    if not row:
        raise HTTPException(404, "Tarea no encontrada")
    return _serialize(row)


@router.post("", status_code=201)
async def create_task(
    body: TaskIn,
    principal: Principal = Depends(get_current_firm),
):
    if body.priority not in VALID_PRIORITY:
        raise HTTPException(400, f"priority inválido (válidos: {sorted(VALID_PRIORITY)})")
    due_at = _parse_dt(body.due_at)
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into tasks
              (firm_id, matter_id, title, description, priority,
               assignee_user_id, due_at, tags,
               source_comment_id, source_lesson_id, source_document_id,
               created_by)
            values ($1::uuid, $2::uuid, $3, $4, $5,
                    $6::uuid, $7, $8,
                    $9::uuid, $10::uuid, $11::uuid, $12::uuid)
            returning *,
              (select titulo from matters where id = tasks.matter_id) as matter_titulo,
              (select full_name from users where id = tasks.assignee_user_id) as assignee_name
            """,
            principal.firm_id, body.matter_id, body.title, body.description,
            body.priority, body.assignee_user_id, due_at, body.tags or [],
            body.source_comment_id, body.source_lesson_id, body.source_document_id,
            principal.user_id,
        )
    return _serialize(row)


@router.patch("/{task_id}")
async def update_task(
    task_id: str,
    body: TaskPatch,
    principal: Principal = Depends(get_current_firm),
):
    if body.status and body.status not in VALID_STATUS:
        raise HTTPException(400, "status inválido")
    if body.priority and body.priority not in VALID_PRIORITY:
        raise HTTPException(400, "priority inválido")
    due_at = _parse_dt(body.due_at) if body.due_at else None

    sets: list[str] = []
    args: list = []
    idx = 1
    for col, val in (
        ("title", body.title), ("description", body.description),
        ("priority", body.priority), ("status", body.status),
    ):
        if val is not None:
            sets.append(f"{col} = ${idx}"); args.append(val); idx += 1
    if body.matter_id is not None:
        sets.append(f"matter_id = ${idx}::uuid"); args.append(body.matter_id or None); idx += 1
    if body.assignee_user_id is not None:
        sets.append(f"assignee_user_id = ${idx}::uuid"); args.append(body.assignee_user_id or None); idx += 1
    if body.due_at is not None:
        sets.append(f"due_at = ${idx}"); args.append(due_at); idx += 1
    if body.tags is not None:
        sets.append(f"tags = ${idx}"); args.append(body.tags); idx += 1
    # Si pasa a done sin pasar status complete, no auto-marcamos completed_*
    # Si pasa status=done explícito → completed_at = now()
    if body.status == "done":
        sets.append(f"completed_at = ${idx}"); args.append(datetime.utcnow()); idx += 1
        sets.append(f"completed_by = ${idx}::uuid"); args.append(principal.user_id); idx += 1
    elif body.status and body.status != "done":
        sets.append("completed_at = null")
        sets.append("completed_by = null")

    if not sets:
        raise HTTPException(400, "Sin cambios")
    args.append(principal.firm_id)
    args.append(task_id)

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            update tasks set {', '.join(sets)}
             where firm_id = ${idx}::uuid and id = ${idx + 1}::uuid
             returning *,
               (select titulo from matters where id = tasks.matter_id) as matter_titulo,
               (select full_name from users where id = tasks.assignee_user_id) as assignee_name
            """,
            *args,
        )
    if not row:
        raise HTTPException(404, "Tarea no encontrada")
    return _serialize(row)


@router.post("/{task_id}/complete")
async def complete_task(
    task_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update tasks
               set status = 'done', completed_at = now(), completed_by = $1::uuid
             where firm_id = $2::uuid and id = $3::uuid
             returning id
            """,
            principal.user_id, principal.firm_id, task_id,
        )
    if not row:
        raise HTTPException(404, "Tarea no encontrada")
    return {"completed": True, "id": str(row["id"])}


@router.post("/{task_id}/reopen")
async def reopen_task(
    task_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            update tasks
               set status = 'open', completed_at = null, completed_by = null
             where firm_id = $1::uuid and id = $2::uuid
            """,
            principal.firm_id, task_id,
        )
    return {"reopened": True}


@router.delete("/{task_id}")
async def delete_task(
    task_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "delete from tasks where firm_id = $1::uuid and id = $2::uuid",
            principal.firm_id, task_id,
        )
    return {"deleted": True}
