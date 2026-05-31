"""Tool 8 · search_brave_gov · búsqueda web limitada a gov.co (Brave Search)."""
from __future__ import annotations

import logging
from typing import Any, Optional

from .base import ToolContext, ToolDef

logger = logging.getLogger(__name__)


class SearchBraveGovTool(ToolDef):
    name = "search_brave_gov"
    description = (
        "★ USAR cuando necesites validar una cita NUEVA o investigar normativa CO "
        "que no esté en el SKILL whitelist ni en los MCP oficiales. Búsqueda en "
        "sitios .gov.co con Brave Search API. Trae top resultados con URL oficial. "
        "Después de cada resultado relevante, validar con verify_citation o "
        "fetch_mcp_official antes de incluir en el documento."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "site_filter": {"type": "string", "default": "gov.co"},
            "limit": {"type": "integer", "default": 5, "maximum": 10},
        },
        "required": ["query"],
    }
    cacheable = True
    cache_ttl_seconds = 86400
    timeout_seconds = 15.0

    def __init__(self, **_: Any):
        pass

    async def run(
        self,
        ctx: ToolContext,
        query: str,
        site_filter: str = "gov.co",
        limit: int = 5,
    ) -> dict:
        limit = max(1, min(10, int(limit)))
        try:
            # Reuso del wrapper existente
            from lex.verify.tools.fetch_web_search_official import _brave_search   # noqa
        except Exception:
            _brave_search = None

        results: list[dict] = []
        if _brave_search is not None:
            try:
                full_query = f"site:{site_filter} {query}" if site_filter else query
                raw = await _brave_search(full_query, count=limit)
                for item in (raw or [])[:limit]:
                    results.append({
                        "title": item.get("title"),
                        "url": item.get("url"),
                        "snippet": (item.get("description") or "")[:300],
                    })
            except Exception as e:
                logger.warning("search_brave_gov failed: %s", e)

        return {
            "query": query,
            "site_filter": site_filter,
            "count": len(results),
            "results": results,
            "_note": "Resultados DEBEN validarse con verify_citation antes de citar.",
        }


def build_tool(**_: Any) -> ToolDef:
    return SearchBraveGovTool()
