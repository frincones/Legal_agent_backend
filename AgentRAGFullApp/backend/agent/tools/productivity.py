"""Sprint 17 · Voice/agent tools de productividad.

  · create_task(title, description?, priority?, due_at?, assignee_user_id?, matter_id?)
  · complete_task(task_id)
  · what_today(horizon_days?)
  · what_is_my_priority()

ctx esperado:
  firm_id, user_id, matter_id (opcional · si está, se asocia)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from agent.tools._ui_events import ui_data_changed

logger = logging.getLogger(__name__)


VALID_PRIORITY = {"low", "normal", "high", "urgent"}


async def create_task_tool(args: dict, ctx: dict) -> dict:
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    # Tolerante: title, description, body, prompt o user_prompt del ctx.
    title = (
        args.get("title") or args.get("description") or args.get("body")
        or args.get("text") or args.get("prompt")
        or ctx.get("user_prompt") or ""
    ).strip()
    if not title:
        return {"error": "Necesito un título para la tarea"}
    # Si recibió el prompt entero (>120 chars), recórtalo a la primera
    # oración o a 120 chars para que sea un título usable.
    if len(title) > 120:
        m = title.split(".", 1)[0].strip()
        if m and len(m) < 120:
            title = m
        else:
            title = title[:117] + "..."
    description = args.get("description") or None
    priority = (args.get("priority") or "normal").strip().lower()
    if priority not in VALID_PRIORITY:
        priority = "normal"
    assignee = args.get("assignee_user_id") or user_id  # por defecto, a sí mismo

    due_at_str = args.get("due_at")
    due_at: Optional[datetime] = None
    if due_at_str:
        try:
            due_at = datetime.fromisoformat(str(due_at_str).replace("Z", "+00:00"))
        except Exception:
            # Fallback: "hoy", "mañana", "viernes" en español?
            lower = str(due_at_str).strip().lower()
            now = datetime.utcnow()
            if lower in ("hoy", "today"):
                due_at = now.replace(hour=23, minute=59, second=0, microsecond=0)
            elif lower in ("mañana", "tomorrow", "manana"):
                due_at = (now + timedelta(days=1)).replace(hour=23, minute=59, second=0, microsecond=0)
            elif lower in ("esta semana", "this week"):
                due_at = (now + timedelta(days=(6 - now.weekday()))).replace(hour=23, minute=59, second=0, microsecond=0)

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "Storage no disponible"}
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into tasks
              (firm_id, matter_id, title, description, priority,
               assignee_user_id, due_at, source, created_by)
            values ($1::uuid, $2::uuid, $3, $4, $5,
                    $6::uuid, $7, 'agent', $8::uuid)
            returning id, title, due_at
            """,
            firm_id, matter_id, title, description, priority, assignee, due_at, user_id,
        )
    return {
        "ok": True,
        "id": str(row["id"]),
        "title": row["title"],
        "due_at": row["due_at"].isoformat() if row["due_at"] else None,
        "message": f"Tarea creada · {row['title']}"
                   + (f" para {row['due_at'].strftime('%d-%b %H:%M')}" if row["due_at"] else ""),
        "_ui_command": ui_data_changed(
            "tasks", matter_id=matter_id, firm_id=firm_id, op="create",
            extra={"task_id": str(row["id"])},
        ),
    }


async def complete_task_tool(args: dict, ctx: dict) -> dict:
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    task_id = (args.get("task_id") or args.get("id") or "").strip()
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    # Si no se pasa task_id, intenta inferir la última task abierta del
    # matter (o del usuario si no hay matter).
    if not task_id:
        from utils.db import get_storage
        storage = await get_storage()
        if not hasattr(storage, "pool"):
            return {"error": "Storage no disponible"}
        async with storage.pool.acquire() as conn:
            if matter_id:
                row = await conn.fetchrow(
                    """select id from tasks
                        where firm_id=$1::uuid and matter_id=$2::uuid
                          and status != 'done'
                        order by created_at desc limit 1""",
                    firm_id, matter_id,
                )
            else:
                row = await conn.fetchrow(
                    """select id from tasks
                        where firm_id=$1::uuid and assignee_user_id=$2::uuid
                          and status != 'done'
                        order by created_at desc limit 1""",
                    firm_id, user_id,
                )
        if not row:
            return {"error": "No encontré tareas abiertas para completar"}
        task_id = str(row["id"])
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "Storage no disponible"}
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update tasks
               set status = 'done', completed_at = now(), completed_by = $1::uuid
             where firm_id = $2::uuid and id = $3::uuid
             returning title
            """,
            user_id, firm_id, task_id,
        )
    if not row:
        return {"error": "Tarea no encontrada"}
    return {
        "ok": True, "message": f"Tarea completada · {row['title']}",
        "_ui_command": ui_data_changed(
            "tasks", firm_id=firm_id, op="update",
            extra={"task_id": task_id, "completed": True},
        ),
    }


async def what_today_tool(args: dict, ctx: dict) -> dict:
    """Devuelve el dashboard My Day del usuario actual."""
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    if not (firm_id and user_id):
        return {"error": "firm_id y user_id requeridos"}
    horizon = int(args.get("horizon_days", 3))
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {}
    async with storage.pool.acquire() as conn:
        raw = await conn.fetchval(
            "select lexai_my_day($1::uuid, $2::uuid, $3)",
            firm_id, user_id, horizon,
        )
    if not raw:
        return {"summary": "Sin actividad pendiente"}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return {"summary": "Sin datos"}
    data = raw if isinstance(raw, dict) else {}
    return {
        "tasks_open_count": data.get("tasks_open_count", 0),
        "mentions_unread_count": data.get("mentions_unread_count", 0),
        "deadlines_count": len(data.get("deadlines_upcoming") or []),
        "top_tasks": [
            {
                "title": t.get("title"),
                "priority": t.get("priority"),
                "due_at": t.get("due_at"),
            }
            for t in (data.get("tasks_open") or [])[:5]
        ],
        "next_deadlines": [
            {"titulo": d.get("titulo"), "fecha": d.get("fecha"), "tipo": d.get("tipo")}
            for d in (data.get("deadlines_upcoming") or [])[:5]
        ],
    }


async def what_is_my_priority_tool(args: dict, ctx: dict) -> dict:
    """¿Qué debería hacer ahora? · combina tarea urgente + plazo vencido."""
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    if not (firm_id and user_id):
        return {"error": "firm_id y user_id requeridos"}
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {}
    async with storage.pool.acquire() as conn:
        overdue_task = await conn.fetchrow(
            """
            select id, title, due_at, priority, matter_id from tasks
             where firm_id = $1::uuid and assignee_user_id = $2::uuid
               and status in ('open','in_progress','blocked')
               and due_at is not null and due_at < now()
             order by case priority
                        when 'urgent' then 1 when 'high' then 2
                        when 'normal' then 3 else 4 end,
                      due_at
             limit 1
            """,
            firm_id, user_id,
        )
        upcoming_deadline = await conn.fetchrow(
            """
            select id, titulo, fecha, tipo, matter_id from matter_deadlines
             where firm_id = $1::uuid and completado = false
               and fecha between now() and now() + interval '3 days'
             order by fecha
             limit 1
            """,
            firm_id,
        )
    if overdue_task:
        return {
            "kind": "overdue_task",
            "summary": f"Tienes una tarea {overdue_task['priority']} vencida: {overdue_task['title']}",
            "task_id": str(overdue_task["id"]),
            "matter_id": str(overdue_task["matter_id"]) if overdue_task["matter_id"] else None,
        }
    if upcoming_deadline:
        return {
            "kind": "upcoming_deadline",
            "summary": f"Plazo próximo: {upcoming_deadline['titulo']} ({upcoming_deadline['fecha'].isoformat() if upcoming_deadline['fecha'] else ''})",
            "deadline_id": str(upcoming_deadline["id"]),
            "matter_id": str(upcoming_deadline["matter_id"]) if upcoming_deadline["matter_id"] else None,
        }
    return {"kind": "all_clear", "summary": "Sin tareas vencidas ni plazos en los próximos 3 días"}
