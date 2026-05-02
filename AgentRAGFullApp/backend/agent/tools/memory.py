"""F5 · Tools de memoria persistente del agente.

remember(key, value, scope?, ttl_days?) - guarda una preferencia o dato
recall(key, scope?) - recupera por key exacto
recall_relevant(query, scope?, top_n=3) - búsqueda semántica
forget(key, scope?) - borra

Scopes:
  firm:    compartido por todo el despacho
  user:    sólo el usuario actual
  matter:  scope_ref = matter_id (vinculado al caso)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _vec_to_pg(v: list[float]) -> str:
    """asyncpg pgvector encoder: list[float] → '[v1,v2,...]' literal."""
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


async def _embed(text: str) -> Optional[list[float]]:
    try:
        from utils.llm import llm_generate_embedding
        return await llm_generate_embedding(text, purpose="agent_memory")
    except Exception as e:
        logger.debug("embedding failed: %s", e)
        return None


async def remember_tool(args: dict, ctx: dict) -> dict:
    """Persiste una memoria (key, value)."""
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    key = (args.get("key") or "").strip()
    value = args.get("value")
    scope = (args.get("scope") or "firm").strip()
    ttl_days = args.get("ttl_days")
    scope_ref = args.get("scope_ref") or ctx.get("matter_id")

    if not key:
        return {"error": "key requerido"}
    if scope not in ("firm", "user", "matter"):
        return {"error": "scope debe ser firm | user | matter"}
    if scope == "matter" and not scope_ref:
        return {"error": "scope='matter' requiere scope_ref (matter_id)"}

    # Embedding del key+value para búsqueda semántica
    text_for_embed = f"{key}: {json.dumps(value, ensure_ascii=False)[:500]}"
    embedding = await _embed(text_for_embed)
    embedding_pg = _vec_to_pg(embedding) if embedding else None

    ttl_until = None
    if ttl_days is not None:
        ttl_until = datetime.now(timezone.utc) + timedelta(days=int(ttl_days))

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}
    async with storage.pool.acquire() as conn:
        if embedding_pg:
            row = await conn.fetchrow(
                """
                insert into agent_memory (firm_id, user_id, scope, scope_ref, key, value, embedding, ttl_until)
                values ($1::uuid, $2::uuid, $3, $4::uuid, $5, $6::jsonb, $7::vector, $8)
                on conflict (firm_id, scope, scope_ref, key) do update set
                  value = excluded.value, embedding = excluded.embedding,
                  ttl_until = excluded.ttl_until, updated_at = now()
                returning id
                """,
                firm_id, user_id if scope == "user" else None,
                scope, scope_ref if scope == "matter" else None,
                key, json.dumps(value), embedding_pg, ttl_until,
            )
        else:
            row = await conn.fetchrow(
                """
                insert into agent_memory (firm_id, user_id, scope, scope_ref, key, value, ttl_until)
                values ($1::uuid, $2::uuid, $3, $4::uuid, $5, $6::jsonb, $7)
                on conflict (firm_id, scope, scope_ref, key) do update set
                  value = excluded.value, ttl_until = excluded.ttl_until, updated_at = now()
                returning id
                """,
                firm_id, user_id if scope == "user" else None,
                scope, scope_ref if scope == "matter" else None,
                key, json.dumps(value), ttl_until,
            )
    return {"id": str(row["id"]), "key": key, "scope": scope, "stored": True}


async def recall_tool(args: dict, ctx: dict) -> dict:
    """Recupera una memoria por key exacto."""
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    key = (args.get("key") or "").strip()
    scope = (args.get("scope") or "firm").strip()
    scope_ref = args.get("scope_ref") or ctx.get("matter_id")
    if not key:
        return {"error": "key requerido"}
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, value, scope, scope_ref, ttl_until, created_at, updated_at
            from agent_memory
            where firm_id = $1::uuid and scope = $2 and key = $3
              and (scope_ref is null or scope_ref = $4::uuid)
              and (ttl_until is null or ttl_until > now())
            order by case when scope_ref is not null then 0 else 1 end
            limit 1
            """,
            firm_id, scope, key, scope_ref,
        )
    if not row:
        return {"key": key, "found": False}
    return {
        "key": key,
        "found": True,
        "value": row["value"],
        "scope": row["scope"],
        "scope_ref": str(row["scope_ref"]) if row["scope_ref"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


async def recall_relevant_tool(args: dict, ctx: dict) -> dict:
    """Búsqueda semántica de memorias relevantes a `query`."""
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    query = (args.get("query") or "").strip()
    top_n = max(1, min(int(args.get("top_n") or 3), 10))
    if not query:
        return {"error": "query requerido"}
    embedding = await _embed(query)
    if not embedding:
        return {"matches": [], "note": "embedding no disponible"}
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}
    embedding_pg = _vec_to_pg(embedding)
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, key, value, scope, scope_ref,
                   1 - (embedding <=> $2::vector) as similarity
            from agent_memory
            where firm_id = $1::uuid
              and embedding is not null
              and (ttl_until is null or ttl_until > now())
            order by embedding <=> $2::vector
            limit $3
            """,
            firm_id, embedding_pg, top_n,
        )
    return {
        "matches": [
            {
                "key": r["key"],
                "value": r["value"],
                "scope": r["scope"],
                "similarity": float(r["similarity"]),
            }
            for r in rows
        ],
    }


async def forget_tool(args: dict, ctx: dict) -> dict:
    """Borra una memoria por key."""
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    key = (args.get("key") or "").strip()
    scope = (args.get("scope") or "firm").strip()
    scope_ref = args.get("scope_ref") or ctx.get("matter_id")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}
    async with storage.pool.acquire() as conn:
        if scope_ref and scope == "matter":
            n = await conn.fetchval(
                "with d as (delete from agent_memory where firm_id = $1::uuid "
                "and scope = $2 and key = $3 and scope_ref = $4::uuid returning 1) "
                "select count(*)::int from d",
                firm_id, scope, key, scope_ref,
            )
        else:
            n = await conn.fetchval(
                "with d as (delete from agent_memory where firm_id = $1::uuid "
                "and scope = $2 and key = $3 and scope_ref is null returning 1) "
                "select count(*)::int from d",
                firm_id, scope, key,
            )
    return {"key": key, "deleted": int(n or 0)}
