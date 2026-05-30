"""Tool 5 · recall_memory · memoria episódica/semántica entre sesiones.

Lee agent_memory (ya existente) con FTS sobre value::text para encontrar
memoria relevante. Equivale a "Claude recuerda lo que dijimos antes".
"""
from __future__ import annotations

import logging
from typing import Any

from .base import ToolContext, ToolDef

logger = logging.getLogger(__name__)


class RecallMemoryTool(ToolDef):
    name = "recall_memory"
    description = (
        "Busca en la memoria del agente (agent_memory) por contenido relevante al query. "
        "Útil cuando el usuario hace referencias ambiguas como 'como el caso anterior' o "
        "'recuerda el contrato que generamos para X'. Retorna hasta `limit` entradas "
        "ordenadas por relevancia."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "kind": {
                "type": "string",
                "enum": ["episodic", "semantic", "system", "any"],
                "default": "any",
            },
            "limit": {"type": "integer", "default": 5, "maximum": 20},
        },
        "required": ["query"],
    }
    cacheable = False
    timeout_seconds = 10.0

    def __init__(self, pool=None, **_: Any):
        self.pool = pool

    async def run(
        self,
        ctx: ToolContext,
        query: str,
        kind: str = "any",
        limit: int = 5,
    ) -> dict:
        pool = self.pool or ctx.pool
        if pool is None or ctx.firm_id is None:
            return {"hits": [], "_warning": "pool o firm_id no disponible"}

        limit = max(1, min(20, int(limit)))
        # M20.11: usa RPC lexai_recall_memory con FTS index. Fallback a ILIKE si RPC no existe.
        scope_filter = None if kind == "any" else kind
        try:
            async with pool.acquire() as conn:
                # Intentar RPC FTS primero (M20.11)
                try:
                    rows = await conn.fetch(
                        "select * from lexai_recall_memory($1::uuid, $2, $3, $4)",
                        ctx.firm_id, query, scope_filter, limit,
                    )
                except Exception as rpc_e:
                    logger.info("lexai_recall_memory RPC no disponible, fallback ILIKE: %s", rpc_e)
                    if scope_filter is None:
                        rows = await conn.fetch(
                            """
                            select id, scope, key, value, created_at, 1.0::real as rank
                            from agent_memory
                            where firm_id = $1
                              and (key ilike '%' || $2 || '%' or value::text ilike '%' || $2 || '%')
                              and (ttl_until is null or ttl_until > now())
                            order by created_at desc
                            limit $3
                            """,
                            ctx.firm_id, query, limit,
                        )
                    else:
                        rows = await conn.fetch(
                            """
                            select id, scope, key, value, created_at, 1.0::real as rank
                            from agent_memory
                            where firm_id = $1 and scope = $2
                              and (key ilike '%' || $3 || '%' or value::text ilike '%' || $3 || '%')
                              and (ttl_until is null or ttl_until > now())
                            order by created_at desc
                            limit $4
                            """,
                            ctx.firm_id, scope_filter, query, limit,
                        )
        except Exception as e:
            logger.warning("recall_memory query failed: %s", e)
            return {"hits": [], "_error": str(e)[:200]}

        hits = [
            {
                "id": str(r["id"]),
                "scope": r["scope"],
                "key": r["key"],
                "value_preview": str(r["value"])[:400],
                "rank": float(r["rank"]) if "rank" in r.keys() else 1.0,
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]
        return {"hits": hits, "count": len(hits), "query": query, "scope_filter": scope_filter}


def build_tool(pool=None, **_: Any) -> ToolDef:
    return RecallMemoryTool(pool=pool)
