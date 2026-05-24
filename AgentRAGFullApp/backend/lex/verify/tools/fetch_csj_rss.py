"""Tool: FetchCSJRss — wraps legal_sources/csj_source.py (creado en M11)."""
from __future__ import annotations

import logging
import time

from lex.verify.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class FetchCSJRss(BaseTool):
    """CSJ search via WordPress RSS feed (cortesuprema.gov.co)."""

    name = "fetch_csj_rss"
    timeout_seconds = 15.0

    async def run(self, parsed) -> ToolResult:
        started = time.time()
        if parsed.kind != "jurisprudencia":
            return self._build_miss("kind_not_jurisprudencia")
        # CSJ maneja SL/SC/SP/STC/STL/STP
        if parsed.tipo not in ("SL", "SC", "SP", "STC", "STL", "STP"):
            return self._build_miss(f"tipo_not_csj:{parsed.tipo}")

        try:
            from legal_sources.csj_source import search_csj
            r = await search_csj(parsed.normalized)
            if not r:
                return ToolResult(
                    tool_name=self.name,
                    status="miss",
                    confidence=0.0,
                    raw_evidence={"reason": "rss_empty"},
                    duration_ms=int((time.time() - started) * 1000),
                )

            mt = r.get("match_type", "mention")
            confidence = 0.92 if mt == "exact" else 0.7
            result = ToolResult(
                tool_name=self.name,
                status="hit",
                confidence=confidence,
                fuente_url=r.get("fuente_url"),
                titulo=r.get("titulo"),
                raw_evidence={
                    "match_type": mt,
                    "items_count": r.get("items_count", 1),
                    "query_used": r.get("query_used"),
                },
                duration_ms=int((time.time() - started) * 1000),
            )
            return self._ensure_fuente_url(result, parsed)
        except Exception as e:
            logger.warning("FetchCSJRss failed for %s: %s", parsed.normalized, e)
            return self._build_error(e)
