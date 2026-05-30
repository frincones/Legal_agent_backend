"""Tool 7 · search_jurisprudence · búsqueda híbrida en jurisprudencia CO.

Wrapper sobre el RPC search_jurisprudencia (pgvector + FTS) + filtros opcionales
por corte/año. NO depende de hunters_stage porque eso requiere TemplateDef;
en su lugar usamos directamente el RPC SQL que ya está en producción.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from utils.embeddings import embed_text

from .base import ToolContext, ToolDef

logger = logging.getLogger(__name__)


VALID_CORTES = {"CC", "CORTE_CONSTITUCIONAL", "CSJ_CIVIL", "CSJ_LABORAL",
                "CSJ_PENAL", "CORTE_SUPREMA", "CE", "CONSEJO_ESTADO"}


class SearchJurisprudenceTool(ToolDef):
    name = "search_jurisprudence"
    description = (
        "Busca jurisprudencia colombiana (Corte Constitucional, CSJ salas civil/laboral/penal, "
        "Consejo de Estado) por tema. Retorna las sentencias más relevantes con ratio_decidendi, "
        "magistrado, fecha y fuente_url. Invocar solo cuando el documento necesite precedentes "
        "(e.g., demanda, recurso, concepto jurídico). NO invocar para poderes o derechos de petición."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Tema o supuesto fáctico a buscar"},
            "corte": {
                "type": "string",
                "description": "Filtro opcional por corte (CC, CSJ_CIVIL, CSJ_LABORAL, CSJ_PENAL, CE).",
            },
            "year_from": {"type": "integer", "description": "Filtro opcional: año mínimo de la sentencia"},
            "solo_precedentes": {"type": "boolean", "default": False},
            "limit": {"type": "integer", "default": 5, "maximum": 20},
        },
        "required": ["query"],
    }
    invokes_llm = True   # internamente embed_text usa OpenAI
    cacheable = True
    cache_ttl_seconds = 3600
    timeout_seconds = 20.0

    def __init__(self, pool=None, openai_client=None, **_: Any):
        self.pool = pool
        self.openai_client = openai_client

    async def run(
        self,
        ctx: ToolContext,
        query: str,
        corte: Optional[str] = None,
        year_from: Optional[int] = None,
        solo_precedentes: bool = False,
        limit: int = 5,
    ) -> dict:
        pool = self.pool or ctx.pool
        if pool is None:
            return {"hits": [], "_warning": "pool no disponible"}

        limit = max(1, min(20, int(limit)))
        corte_filter = corte.upper() if corte and corte.upper() in VALID_CORTES else None

        try:
            embedding = await embed_text(query)
        except Exception as e:
            logger.warning("search_jurisprudence embedding failed: %s", e)
            embedding = None

        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    select id, corte, sala, tipo_sentencia, numero, expediente,
                           fecha, magistrado_ponente, temas,
                           es_precedente, ratio_decidendi, fuente_url, texto_preview
                    from search_jurisprudencia(
                      $1::vector(1536), $2::text, $3::text, $4::text, $5::boolean, $6::int, $7::int
                    )
                    """,
                    embedding,
                    query,
                    corte_filter,
                    None,        # filter_tipo
                    bool(solo_precedentes),
                    year_from,
                    limit,
                )
        except Exception as e:
            logger.warning("search_jurisprudencia RPC failed (fallback empty): %s", e)
            return {"hits": [], "_warning": str(e)[:200]}

        hits = []
        for r in rows:
            hits.append({
                "id": str(r["id"]),
                "corte": r["corte"],
                "sala": r.get("sala") if isinstance(r, dict) else r["sala"],
                "tipo": r["tipo_sentencia"],
                "numero": r["numero"],
                "fecha": str(r["fecha"]) if r["fecha"] else None,
                "magistrado": r["magistrado_ponente"],
                "ratio": (r["ratio_decidendi"] or "")[:600],
                "fuente_url": r["fuente_url"],
                "es_precedente": bool(r["es_precedente"]) if r["es_precedente"] is not None else None,
            })
        return {"query": query, "count": len(hits), "hits": hits}


def build_tool(pool=None, openai_client=None, **_: Any) -> ToolDef:
    return SearchJurisprudenceTool(pool=pool, openai_client=openai_client)
