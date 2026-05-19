"""Sprint 22 · Voice tools de wizards públicos.

  · list_wizards()                              · enumera wizards system disponibles
  · start_wizard(slug)                          · crea session anónima y devuelve token
  · wizard_session_status(session_token)        · consulta progreso
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def list_wizards_tool(args: dict, ctx: dict) -> dict:
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch("select * from lexai_wizard_system_list()")
    return {
        "items": [
            {
                "slug": r["slug"],
                "name": r["name"],
                "category": r["category"],
                "description": r["description"],
                "public_url_path": f"/tramites/{r['slug']}",
            }
            for r in rows
        ],
        "count": len(rows),
    }


async def start_wizard_tool(args: dict, ctx: dict) -> dict:
    slug = (args.get("slug") or args.get("name") or args.get("wizard") or "").strip().lower()
    from utils.db import get_storage
    from utils.wizard_helpers import random_token
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "Storage no disponible"}
    # Fallback: extrae nombre del wizard del prompt o busca por nombre parcial.
    if not slug:
        prompt_str = (args.get("prompt") or ctx.get("user_prompt") or "").lower()
        # Patrones comunes: "wizard de tutela", "el wizard tutela", "wizard X"
        import re
        m = re.search(r"wizard\s+(?:de\s+|del\s+)?([a-záéíóúñ_-]+)", prompt_str)
        if m:
            slug = m.group(1).strip()
    tpl = None
    async with storage.pool.acquire() as conn:
        if slug:
            tpl = await conn.fetchrow(
                "select * from lexai_wizard_template_by_slug($1)", slug
            )
            if not tpl:
                # busca match parcial por slug o nombre
                tpl = await conn.fetchrow(
                    """select * from wizard_templates
                        where active = true
                          and (slug ilike '%'||$1||'%' or name ilike '%'||$1||'%')
                        limit 1""",
                    slug,
                )
        if not tpl:
            # último fallback: el primer wizard activo
            tpl = await conn.fetchrow(
                "select * from wizard_templates where active=true order by created_at limit 1"
            )
        if not tpl:
            return {"error": f"Wizard '{slug or 'sin slug'}' no encontrado o inactivo"}
        token = random_token()
        await conn.fetchrow(
            """
            insert into wizard_sessions
              (wizard_template_id, session_token, routed_to_firm_id, document_title)
            values ($1::uuid, $2, $3::uuid, $4)
            returning id
            """,
            tpl["id"], token, tpl["firm_id"], tpl["document_title"],
        )
    return {
        "ok": True,
        "session_token": token,
        "wizard_slug": slug,
        "wizard_name": tpl["name"],
        "public_url_path": f"/tramites/{slug}?token={token}",
    }


async def wizard_session_status_tool(args: dict, ctx: dict) -> dict:
    token = (args.get("session_token") or args.get("token") or "").strip()
    if not token:
        return {"error": "Necesito session_token"}
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "Storage no disponible"}
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select s.id, s.current_step, s.status, s.completed_steps,
                   t.name as template_name, t.slug as template_slug
              from wizard_sessions s
              join wizard_templates t on t.id = s.wizard_template_id
             where s.session_token = $1
            """,
            token,
        )
    if not row:
        return {"error": "Session no encontrada"}
    return {
        "session_id": str(row["id"]),
        "template_slug": row["template_slug"],
        "template_name": row["template_name"],
        "current_step": int(row["current_step"] or 0),
        "completed_steps": list(row["completed_steps"] or []),
        "status": row["status"],
    }
