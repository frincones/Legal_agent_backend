"""Sprint 16 · Voice/agent tools de colaboración.

  · add_comment(body, anchor_kind?, matter_id?, matter_document_id?, ...)
  · resolve_comment(comment_id)
  · show_activity(matter_id?, limit?)
  · show_active_users(matter_id?)

ctx esperado:
  firm_id, user_id, matter_id (opcional · si está, se usa como anchor por defecto)
"""

from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


VALID_ANCHORS = {"matter", "matter_document", "canvas", "lesson", "kb_entry"}


async def add_comment_tool(args: dict, ctx: dict) -> dict:
    """Crea un comentario · si no se da anchor_kind, asume 'matter' del contexto."""
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    matter_document_id = args.get("matter_document_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    # Tolerante: body, text, content, prompt o user_prompt del ctx.
    body = (
        args.get("body") or args.get("text") or args.get("content")
        or args.get("prompt") or ctx.get("user_prompt") or ""
    ).strip()
    if not body:
        return {"error": "Necesito el texto del comentario"}

    anchor_kind = (args.get("anchor_kind") or "").strip()
    if not anchor_kind:
        if matter_document_id:
            anchor_kind = "matter_document"
        elif matter_id:
            anchor_kind = "matter"
        else:
            return {"error": "No sé en qué caso comentar. Dame matter_id o entra a un caso."}
    if anchor_kind not in VALID_ANCHORS:
        return {"error": f"anchor_kind inválido ({sorted(VALID_ANCHORS)})"}

    from utils.db import get_storage
    from utils.mentions import resolve_mentions, render_preview
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "Storage no disponible"}

    async with storage.pool.acquire() as conn:
        mention_ids = await resolve_mentions(conn, firm_id, body)
        row = await conn.fetchrow(
            """
            insert into comments
              (firm_id, anchor_kind, matter_id, matter_document_id, body, mentions, created_by)
            values ($1::uuid, $2, $3::uuid, $4::uuid, $5, $6::uuid[], $7::uuid)
            returning id, body
            """,
            firm_id, anchor_kind, matter_id, matter_document_id, body,
            mention_ids, user_id,
        )

    # Push notifications best-effort (mismo helper que el endpoint REST)
    if mention_ids:
        try:
            from api.push import dispatch_to_user
            preview = render_preview(body)
            for uid in mention_ids:
                await dispatch_to_user(
                    user_id=uid, firm_id=firm_id,
                    title="Te mencionaron",
                    body=preview[:160],
                    url=f"/casos/{matter_id}?comment={row['id']}" if matter_id else "/actividad",
                )
        except Exception as e:
            logger.debug("voice add_comment push failed: %s", e)

    from agent.tools._ui_events import ui_data_changed
    return {
        "ok": True,
        "id": str(row["id"]),
        "mentions_count": len(mention_ids),
        "message": f"Comentario guardado{' · mencionaste a ' + str(len(mention_ids)) + ' persona(s)' if mention_ids else ''}",
        "_ui_command": ui_data_changed(
            "comments", matter_id=matter_id, firm_id=firm_id, op="create",
            extra={"comment_id": str(row["id"]), "mentions_count": len(mention_ids)},
        ),
    }


async def resolve_comment_tool(args: dict, ctx: dict) -> dict:
    """Resuelve un comentario por id."""
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    comment_id = (args.get("comment_id") or args.get("id") or "").strip()
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    if not comment_id:
        # Infier el último comentario sin resolver del matter o user.
        from utils.db import get_storage
        storage = await get_storage()
        if not hasattr(storage, "pool"):
            return {"error": "Storage no disponible"}
        async with storage.pool.acquire() as conn:
            if matter_id:
                row = await conn.fetchrow(
                    """select id from comments
                        where firm_id=$1::uuid and matter_id=$2::uuid
                          and resolved is not true
                        order by created_at desc limit 1""",
                    firm_id, matter_id,
                )
            else:
                row = await conn.fetchrow(
                    """select id from comments
                        where firm_id=$1::uuid and created_by=$2::uuid
                          and resolved is not true
                        order by created_at desc limit 1""",
                    firm_id, user_id,
                )
        if not row:
            return {"error": "No encontré comentarios sin resolver"}
        comment_id = str(row["id"])
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "Storage no disponible"}
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update comments
               set resolved = true, resolved_by = $1::uuid, resolved_at = now()
             where firm_id = $2::uuid and id = $3::uuid
             returning id
            """,
            user_id, firm_id, comment_id,
        )
    if not row:
        return {"error": "Comentario no encontrado"}
    from agent.tools._ui_events import ui_data_changed
    return {
        "ok": True, "message": "Comentario resuelto",
        "_ui_command": ui_data_changed(
            "comments", firm_id=firm_id, op="update",
            extra={"comment_id": comment_id, "resolved": True},
        ),
    }


async def show_activity_tool(args: dict, ctx: dict) -> dict:
    """Devuelve el feed de actividad reciente (matter en ctx o args.matter_id)."""
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    limit = min(int(args.get("limit", 10)), 30)
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select * from lexai_activity_feed($1::uuid, $2::uuid, null, null, null, $3)
            """,
            firm_id, matter_id, limit,
        )
    return {
        "items": [
            {
                "id": str(r["id"]),
                "ts": r["ts"].isoformat() if r["ts"] else None,
                "kind": r["kind"],
                "title": r["title"],
                "preview": r["preview"],
                "actor_name": r["actor_name"],
                "matter_id": str(r["matter_id"]) if r["matter_id"] else None,
            }
            for r in rows
        ],
        "count": len(rows),
    }


async def show_active_users_tool(args: dict, ctx: dict) -> dict:
    """¿Quién más está mirando este caso ahora?"""
    firm_id = ctx.get("firm_id")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    if not (firm_id and matter_id):
        return {"error": "Necesito firm_id y matter_id"}
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            "select * from lexai_active_users($1::uuid, $2::uuid, 90)",
            firm_id, matter_id,
        )
    return {
        "items": [
            {
                "user_id": str(r["user_id"]),
                "full_name": r["full_name"],
                "location_kind": r["location_kind"],
            }
            for r in rows
        ],
        "count": len(rows),
    }
