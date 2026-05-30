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
        # FTS simple (sin índice GIN aún → S1.5 puede agregarlo). Usa ILIKE como fallback.
        try:
            async with pool.acquire() as conn:
                if kind == "any":
                    rows = await conn.fetch(
                        """
                        select id, kind, key, value, created_at
                        from agent_memory
                        where firm_id = $1
                          and (key ilike '%' || $2 || '%' or value::text ilike '%' || $2 || '%')
                          and (expires_at is null or expires_at > now())
                        order by created_at desc
                        limit $3
                        """,
                        ctx.firm_id, query, limit,
                    )
                else:
                    rows = await conn.fetch(
                        """
                        select id, kind, key, value, created_at
                        from agent_memory
                        where firm_id = $1 and kind = $2
                          and (key ilike '%' || $3 || '%' or value::text ilike '%' || $3 || '%')
                          and (expires_at is null or expires_at > now())
                        order by created_at desc
                        limit $4
                        """,
                        ctx.firm_id, kind, query, limit,
                    )
        except Exception as e:
            logger.warning("recall_memory query failed: %s", e)
            return {"hits": [], "_error": str(e)[:200]}

        hits = [
            {
                "id": str(r["id"]),
                "kind": r["kind"],
                "key": r["key"],
                "value_preview": str(r["value"])[:400],
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]
        return {"hits": hits, "count": len(hits), "query": query, "kind_filter": kind}


def build_tool(pool=None, **_: Any) -> ToolDef:
    return RecallMemoryTool(pool=pool)
