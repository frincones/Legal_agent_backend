"""Tool: FetchWebSearchOfficial — último recurso para fuente_url.

Wrap de legal_sources/web_search.search_web restringido a dominios oficiales.
Solo se invoca cuando las otras tools fallaron Y necesitamos garantizar una
URL al usuario.
"""
from __future__ import annotations

import logging
import time

from lex.verify.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class FetchWebSearchOfficial(BaseTool):
    """Web search restringido a dominios oficiales gov.co."""

    name = "fetch_web_search_official"
    timeout_seconds = 12.0

    async def run(self, parsed) -> ToolResult:
        started = time.time()
        if not parsed:
            return self._build_miss("no_parsed")

        try:
            from legal_sources.web_search import search_web
            query = parsed.normalized or parsed.raw
            results = await search_web(query, limit=3, site_filter=True)
            if not results:
                return ToolResult(
                    tool_name=self.name,
                    status="miss",
                    confidence=0.0,
                    raw_evidence={"reason": "no_results"},
                    duration_ms=int((time.time() - started) * 1000),
                )

            best = results[0]
            return ToolResult(
                tool_name=self.name,
                status="hit",
                confidence=0.65,  # confidence media — es búsqueda, no match exacto
                fuente_url=best.get("url"),
                titulo=best.get("title", "")[:200],
                raw_evidence={
                    "snippet": (best.get("snippet") or "")[:300],
                    "results_count": len(results),
                },
                duration_ms=int((time.time() - started) * 1000),
            )
        except Exception as e:
            logger.warning("FetchWebSearchOfficial failed for %s: %s", parsed.normalized if parsed else "?", e)
            return self._build_error(e)
