"""Tool 4 · load_matter_context · contexto completo del matter activo.

Equivalente a "matter workspace" de Claude for Legal. Retorna en una sola
llamada: matter + parties + timeline + deadlines + risks + documents.

Si no existe el RPC lexai_matter_full_context (S1.5 migración), hace
queries individuales como fallback.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .base import ToolContext, ToolDef

logger = logging.getLogger(__name__)


class LoadMatterContextTool(ToolDef):
    name = "load_matter_context"
    description = (
        "Carga el contexto completo del matter (caso legal) activo: datos del "
        "matter, partes, timeline reciente, deadlines próximos, riesgos abiertos "
        "y documentos. Llamar cuando el usuario está trabajando dentro de un "
        "matter específico (matter_id presente) y la generación se beneficiaría "
        "de ese contexto (e.g., redactar contestación de demanda)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "matter_id": {"type": "string"},
            "include_documents": {"type": "boolean", "default": True},
            "include_timeline": {"type": "boolean", "default": True},
            "timeline_limit": {"type": "integer", "default": 20},
        },
        "required": ["matter_id"],
    }
    cacheable = True
    cache_ttl_seconds = 60
    timeout_seconds = 15.0

    def __init__(self, pool=None, **_: Any):
        self.pool = pool

    async def run(
        self,
        ctx: ToolContext,
        matter_id: str,
        include_documents: bool = True,
        include_timeline: bool = True,
        timeline_limit: int = 20,
    ) -> dict:
        pool = self.pool or ctx.pool
        if pool is None:
            return {"matter_id": matter_id, "_warning": "pool no disponible"}

        # Intentar RPC consolidado (S1.5 migración); si no existe, fallback queries individuales
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchval(
                    "select lexai_matter_full_context($1::uuid)", matter_id,
                )
                if row:
                    data = row if isinstance(row, dict) else json.loads(row)
                    return data
        except Exception as e:
            logger.info("lexai_matter_full_context RPC no disponible, usando fallback: %s", e)

        return await self._fallback_queries(pool, matter_id, include_documents,
                                              include_timeline, timeline_limit)

    async def _fallback_queries(
        self, pool, matter_id: str, include_docs: bool,
        include_tl: bool, tl_limit: int,
    ) -> dict:
        async with pool.acquire() as conn:
            matter = await conn.fetchrow(
                """
                select id, titulo, materia, etapa_procesal, tribunal, juzgado,
                       expediente, status, priority, cuantia, cuantia_currency,
                       proxima_fecha, proxima_tipo
                from matters
                where id = $1::uuid
                limit 1
                """,
                matter_id,
            )
            if not matter:
                return {"matter_id": matter_id, "_warning": "matter no encontrado"}

            parties = await conn.fetch(
                """
                select id, rol, nombre, tax_id, origen
                from matter_parties
                where matter_id = $1::uuid
                limit 20
                """,
                matter_id,
            )
            deadlines = await conn.fetch(
                """
                select id, titulo, fecha, tipo, completado
                from matter_deadlines
                where matter_id = $1::uuid and (completado is null or completado = false)
                order by fecha asc nulls last
                limit 5
                """,
                matter_id,
            )
            risks = []
            try:
                risks = await conn.fetch(
                    """
                    select id, type, severity, title, description, mitigation
                    from case_risks
                    where matter_id = $1::uuid and resolved_at is null
                    order by severity desc
                    limit 5
                    """,
                    matter_id,
                )
            except Exception:
                pass

            timeline = []
            if include_tl:
                try:
                    timeline = await conn.fetch(
                        """
                        select ts, kind, payload
                        from matter_timeline
                        where matter_id = $1::uuid
                        order by ts desc
                        limit $2
                        """,
                        matter_id, tl_limit,
                    )
                except Exception:
                    pass

            documents = []
            if include_docs:
                try:
                    documents = await conn.fetch(
                        """
                        select id, kind, titulo, status, resumen_ia, byte_size
                        from matter_documents
                        where matter_id = $1::uuid
                        order by created_at desc
                        limit 10
                        """,
                        matter_id,
                    )
                except Exception:
                    pass

        return {
            "matter": dict(matter),
            "parties": [dict(p) for p in parties],
            "deadlines": [dict(d) for d in deadlines],
            "risks": [dict(r) for r in risks],
            "timeline": [dict(t) for t in timeline],
            "documents": [dict(d) for d in documents],
        }


def build_tool(pool=None, **_: Any) -> ToolDef:
    return LoadMatterContextTool(pool=pool)
