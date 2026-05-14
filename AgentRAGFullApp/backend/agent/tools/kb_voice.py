"""Sprint 15 · Voice/agent tools para Knowledge Base + Memoria del despacho.

  · search_kb(query, kind?)               · busca en knowledge_entries (RPC híbrida)
  · add_to_kb(title, body, kind?, tags?)  · crea una entrada rápida desde la voz
  · search_lessons(query, outcome?)       · busca en case_lessons (cosine)

ctx esperado:
  firm_id, user_id, matter_id (opcional · si está, se asocia como source_matter_id)
"""

from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


VALID_KINDS = {
    "note", "precedent", "strategy", "template_comment", "citation_note",
    "lesson_learned", "procedure", "case_summary", "contact_note",
}
VALID_OUTCOMES = {"won", "lost", "settled", "abandoned", "unknown"}


async def search_kb_voice_tool(args: dict, ctx: dict) -> dict:
    """Busca en la KB del despacho."""
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    query = (args.get("query") or args.get("q") or "").strip()
    if not query:
        return {"error": "Necesito un término de búsqueda"}
    kind = args.get("kind")
    if kind and kind not in VALID_KINDS:
        return {"error": f"kind inválido (válidos: {sorted(VALID_KINDS)})"}
    limit = min(int(args.get("limit", 10)), 20)

    from utils.db import get_storage
    from utils.embeddings import embed_text_as_pg
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"matches": [], "note": "Storage no disponible"}

    embedding_pg = await embed_text_as_pg(
        query, purpose="kb_voice_search", session_id=str(user_id) if user_id else "",
    )

    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select * from lexai_kb_search(
              $1::uuid, $2,
              case when $3::text is not null then $3::vector else null end,
              $4, $5
            )
            """,
            firm_id, query, embedding_pg, kind, limit,
        )

    matches = [
        {
            "id": str(r["id"]),
            "title": r["title"],
            "snippet": (r["body"] or "")[:280],
            "kind": r["kind"],
            "tags": list(r["tags"] or []),
            "pinned": bool(r["pinned"]),
            "rank": float(r["rank"] or 0),
        }
        for r in rows
    ]
    return {"matches": matches, "count": len(matches), "vector_used": embedding_pg is not None}


async def add_to_kb_voice_tool(args: dict, ctx: dict) -> dict:
    """Crea una entrada rápida en la KB · embed best-effort."""
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    matter_id = ctx.get("matter_id") or args.get("matter_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    title = (args.get("title") or "").strip()
    body = (args.get("body") or args.get("text") or "").strip()
    if not title or not body:
        return {"error": "Necesito title y body"}
    kind = (args.get("kind") or "note").strip()
    if kind not in VALID_KINDS:
        kind = "note"
    tags = args.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    from utils.db import get_storage
    from utils.embeddings import embed_text_as_pg, compose_kb_text
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "Storage no disponible"}

    embedding_pg = await embed_text_as_pg(
        compose_kb_text(title, None, body),
        purpose="kb_voice_add",
        session_id=str(user_id) if user_id else "",
    )

    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into knowledge_entries
              (firm_id, kind, title, body, tags, source_matter_id, visibility,
               embedding, embedding_at, created_by)
            values ($1::uuid, $2, $3, $4, $5, $6::uuid, 'firm',
                    case when $7::text is not null then $7::vector else null end,
                    case when $7::text is not null then now() else null end,
                    $8::uuid)
            returning id, title, kind
            """,
            firm_id, kind, title, body, tags, matter_id,
            embedding_pg, user_id,
        )

    return {
        "ok": True,
        "id": str(row["id"]),
        "title": row["title"],
        "kind": row["kind"],
        "embedded": embedding_pg is not None,
        "message": f"Guardado en KB · {row['title']}",
    }


async def search_lessons_voice_tool(args: dict, ctx: dict) -> dict:
    """Busca lecciones aprendidas similares (case_lessons · cosine)."""
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    query = (args.get("query") or args.get("q") or "").strip()
    if not query:
        return {"error": "Necesito un término de búsqueda"}
    outcome = args.get("outcome")
    if outcome and outcome not in VALID_OUTCOMES:
        return {"error": f"outcome inválido (válidos: {sorted(VALID_OUTCOMES)})"}
    limit = min(int(args.get("limit", 5)), 15)

    from utils.db import get_storage
    from utils.embeddings import embed_text_as_pg
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"matches": [], "note": "Storage no disponible"}

    embedding_pg = await embed_text_as_pg(
        query, purpose="lessons_voice_search", session_id=str(user_id) if user_id else "",
    )
    if not embedding_pg:
        return {"matches": [], "note": "Embedding no disponible · búsqueda semántica deshabilitada"}

    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select * from lexai_lessons_search($1::uuid, $2::vector, $3, $4)
            """,
            firm_id, embedding_pg, outcome, limit,
        )

    matches = [
        {
            "id": str(r["id"]),
            "matter_id": str(r["matter_id"]),
            "outcome": r["outcome"],
            "summary": r["summary"],
            "strategy_used": r["strategy_used"],
            "what_worked": r["what_worked"],
            "tags": list(r["tags"] or []),
            "similarity": float(r["similarity"] or 0),
        }
        for r in rows
    ]
    return {"matches": matches, "count": len(matches)}
