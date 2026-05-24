"""Tool: FetchSenadoSuin — wraps legal_sources/senado_scraper.py."""
from __future__ import annotations

import logging
import time

from lex.verify.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class FetchSenadoSuin(BaseTool):
    """Live fetch de leyes/decretos via SUIN-Juriscol / secretariasenado.gov.co."""

    name = "fetch_senado_suin"
    timeout_seconds = 12.0

    async def run(self, parsed) -> ToolResult:
        started = time.time()
        if parsed.kind not in ("ley", "decreto"):
            return self._build_miss(f"kind_not_supported:{parsed.kind}")
        if parsed.numero is None or parsed.anio is None:
            return self._build_miss("incomplete_parse")

        try:
            from legal_sources.senado_scraper import SenadoSource
            source = SenadoSource()
            try:
                fetched = await source.fetch_norm(parsed.tipo, parsed.numero, parsed.anio)
            finally:
                if hasattr(source, "close"):
                    await source.close()

            if not fetched:
                return ToolResult(
                    tool_name=self.name,
                    status="miss",
                    confidence=0.0,
                    raw_evidence={"reason": "not_found"},
                    duration_ms=int((time.time() - started) * 1000),
                )

            result = ToolResult(
                tool_name=self.name,
                status="hit",
                confidence=0.96,
                fuente_url=fetched.get("fuente_url") or fetched.get("url"),
                titulo=fetched.get("titulo"),
                raw_evidence={
                    "vigencia": fetched.get("vigencia"),
                    "char_count": fetched.get("metadata", {}).get("char_count", 0),
                },
                duration_ms=int((time.time() - started) * 1000),
            )
            return self._ensure_fuente_url(result, parsed)
        except Exception as e:
            logger.warning("FetchSenadoSuin failed for %s: %s", parsed.normalized, e)
            return self._build_error(e)
