"""Sprint M · Matter management tools.

Cinco tools que cubren huecos detectados en la auditoría agent-ui-sync:
el agente decía poder "etiquetar como urgente", "archivar caso" o
"cambiar etapa" pero NO existían como tools registradas — el LLM
alucinaba el éxito o respondía "no tengo esa capacidad".

  · set_matter_priority(matter_id, priority)
  · tag_matter(matter_id, tag) — append a matters.metadata.tags
  · update_matter_etapa(matter_id, etapa)
  · archive_matter(matter_id, reason?) — soft delete via status='archivado'
  · create_matter(titulo, materia, client_id?, ...) — nuevo caso

Todas emiten `_ui_command: data_changed` con resource='matters' para que
las listas (/casos, /inicio, /mi-dia) se refresquen automáticamente.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from agent.tools._ui_events import ui_data_changed

logger = logging.getLogger(__name__)


VALID_PRIORITIES = {"baja", "media", "alta", "critica", "urgente"}
VALID_MATERIAS = {
    "civil", "comercial", "laboral", "familia", "penal", "administrativo",
    "tributario", "constitucional", "ambiental", "otro",
}


async def set_matter_priority_tool(args: dict, ctx: dict) -> dict:
    """Cambia la prioridad de un caso (baja/media/alta/critica/urgente)."""
    firm_id = ctx.get("firm_id")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    priority = (args.get("priority") or "").strip().lower()
    if not (firm_id and matter_id):
        return {"error": "firm_id y matter_id requeridos"}
    if priority not in VALID_PRIORITIES:
        return {
            "error": f"priority inválida · usa una de {sorted(VALID_PRIORITIES)}",
        }
    # Normalize 'urgente' → 'critica' to match enum if needed.
    db_priority = "critica" if priority == "urgente" else priority

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update matters
               set priority = $1,
                   updated_at = now()
             where id = $2::uuid and firm_id = $3::uuid
             returning id, titulo, priority
            """,
            db_priority, matter_id, firm_id,
        )
    if not row:
        return {"error": "caso no encontrado"}
    return {
        "ok": True,
        "matter_id": str(row["id"]),
        "titulo": row["titulo"],
        "priority": row["priority"],
        "_ui_command": ui_data_changed(
            "matters", matter_id=matter_id, firm_id=firm_id, op="update",
            extra={"priority": row["priority"]},
        ),
    }


async def tag_matter_tool(args: dict, ctx: dict) -> dict:
    """Añade un tag al array matters.metadata.tags (sin duplicar)."""
    firm_id = ctx.get("firm_id")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    tag = (args.get("tag") or "").strip()
    if not (firm_id and matter_id and tag):
        return {"error": "firm_id, matter_id y tag requeridos"}
    if len(tag) > 60:
        return {"error": "tag demasiado largo (max 60 chars)"}

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}
    async with storage.pool.acquire() as conn:
        # jsonb_array_elements_text + filter para evitar duplicados
        row = await conn.fetchrow(
            """
            update matters
               set metadata = coalesce(metadata, '{}'::jsonb) ||
                              jsonb_build_object(
                                'tags',
                                (
                                  select coalesce(jsonb_agg(distinct t), '[]'::jsonb)
                                  from (
                                    select jsonb_array_elements_text(
                                      coalesce(metadata->'tags', '[]'::jsonb)
                                    ) as t
                                    union
                                    select $1::text as t
                                  ) merged
                                )
                              ),
                   updated_at = now()
             where id = $2::uuid and firm_id = $3::uuid
             returning id, titulo, metadata->'tags' as tags
            """,
            tag, matter_id, firm_id,
        )
    if not row:
        return {"error": "caso no encontrado"}
    tags = row["tags"]
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            tags = []
    return {
        "ok": True,
        "matter_id": str(row["id"]),
        "titulo": row["titulo"],
        "tag_added": tag,
        "tags": tags or [],
        "_ui_command": ui_data_changed(
            "matters", matter_id=matter_id, firm_id=firm_id, op="update",
            extra={"tag_added": tag},
        ),
    }


async def update_matter_etapa_tool(args: dict, ctx: dict) -> dict:
    """Cambia la etapa procesal del caso (texto libre · ej: 'apelación')."""
    firm_id = ctx.get("firm_id")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    etapa = (args.get("etapa") or "").strip()
    if not (firm_id and matter_id and etapa):
        return {"error": "firm_id, matter_id y etapa requeridos"}
    if len(etapa) > 80:
        return {"error": "etapa demasiado larga (max 80 chars)"}

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update matters
               set etapa = $1, updated_at = now()
             where id = $2::uuid and firm_id = $3::uuid
             returning id, titulo, etapa
            """,
            etapa, matter_id, firm_id,
        )
        # Best-effort timeline entry
        try:
            await conn.execute(
                """
                insert into matter_timeline (matter_id, firm_id, kind, ts, payload)
                values ($1::uuid, $2::uuid, 'etapa_changed', now(), $3::jsonb)
                """,
                matter_id, firm_id, json.dumps({"etapa": etapa}),
            )
        except Exception:
            pass
    if not row:
        return {"error": "caso no encontrado"}
    return {
        "ok": True,
        "matter_id": str(row["id"]),
        "titulo": row["titulo"],
        "etapa": row["etapa"],
        "_ui_command": ui_data_changed(
            "matters", matter_id=matter_id, firm_id=firm_id, op="update",
            extra={"etapa": etapa},
        ),
    }


async def archive_matter_tool(args: dict, ctx: dict) -> dict:
    """Archiva (soft-delete) un caso · status='archivado'."""
    firm_id = ctx.get("firm_id")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    reason = (args.get("reason") or "").strip()
    if not (firm_id and matter_id):
        return {"error": "firm_id y matter_id requeridos"}

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update matters
               set status = 'archivado',
                   metadata = coalesce(metadata, '{}'::jsonb) ||
                              jsonb_build_object(
                                'archived_at', to_jsonb(now()::text),
                                'archived_reason', $1::text
                              ),
                   updated_at = now()
             where id = $2::uuid and firm_id = $3::uuid
             returning id, titulo, status
            """,
            reason or None, matter_id, firm_id,
        )
        try:
            await conn.execute(
                """
                insert into matter_timeline (matter_id, firm_id, kind, ts, payload)
                values ($1::uuid, $2::uuid, 'archived', now(), $3::jsonb)
                """,
                matter_id, firm_id, json.dumps({"reason": reason}),
            )
        except Exception:
            pass
    if not row:
        return {"error": "caso no encontrado"}
    return {
        "ok": True,
        "matter_id": str(row["id"]),
        "titulo": row["titulo"],
        "status": row["status"],
        "_ui_command": ui_data_changed(
            "matters", matter_id=matter_id, firm_id=firm_id, op="update",
            extra={"archived": True, "reason": reason},
        ),
    }


async def create_matter_tool(args: dict, ctx: dict) -> dict:
    """Crea un nuevo caso (matter) · útil para intake rápido por voz/chat."""
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    titulo = (args.get("titulo") or args.get("title") or "").strip()
    materia = (args.get("materia") or "otro").strip().lower()
    if not (firm_id and titulo):
        return {"error": "firm_id y titulo requeridos"}
    if materia not in VALID_MATERIAS:
        return {"error": f"materia inválida · usa una de {sorted(VALID_MATERIAS)}"}
    client_id = args.get("client_id")
    tribunal = (args.get("tribunal") or "").strip() or None
    priority = (args.get("priority") or "media").strip().lower()
    if priority not in VALID_PRIORITIES:
        priority = "media"
    db_priority = "critica" if priority == "urgente" else priority

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into matters
              (firm_id, client_id, titulo, materia, tribunal, priority,
               status, created_by)
            values
              ($1::uuid, $2::uuid, $3, $4, $5, $6, 'activo', $7::uuid)
            returning id, titulo, materia, priority, status, display_id
            """,
            firm_id, client_id, titulo, materia, tribunal,
            db_priority, user_id,
        )
    return {
        "ok": True,
        "matter_id": str(row["id"]),
        "display_id": row.get("display_id"),
        "titulo": row["titulo"],
        "materia": row["materia"],
        "priority": row["priority"],
        "status": row["status"],
        "_ui_command": ui_data_changed(
            "matters", matter_id=str(row["id"]), firm_id=firm_id, op="create",
            extra={"display_id": row.get("display_id")},
        ),
    }
