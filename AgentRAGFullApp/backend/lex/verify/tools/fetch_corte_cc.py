"""Tool: FetchCorteCC — wraps legal_sources/corte_constitucional.py."""
from __future__ import annotations

import logging
import time

from lex.verify.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class FetchCorteCC(BaseTool):
    """Live fetch de sentencia Corte CC con URL predecible."""

    name = "fetch_corte_cc"
    timeout_seconds = 12.0

    async def run(self, parsed) -> ToolResult:
        started = time.time()
        if parsed.kind != "jurisprudencia":
            return self._build_miss("kind_not_jurisprudencia")
        if not parsed.tipo or parsed.numero is None or parsed.anio is None:
            return self._build_miss("incomplete_parse")

        try:
            from legal_sources.corte_constitucional import CorteConstitucionalSource
            source = CorteConstitucionalSource()
            try:
                fetched = await source.fetch_sentencia(parsed.tipo, parsed.numero, parsed.anio)
            finally:
                await source.close()

            if not fetched:
                return ToolResult(
                    tool_name=self.name,
                    status="miss",
                    confidence=0.0,
                    raw_evidence={"reason": "soft_404"},
                    duration_ms=int((time.time() - started) * 1000),
                )

            result = ToolResult(
                tool_name=self.name,
                status="hit",
                confidence=0.97,
                fuente_url=fetched.get("fuente_url"),
                titulo=fetched.get("titulo"),
                raw_evidence={
                    "magistrado": fetched.get("magistrado"),
                    "char_count": fetched.get("metadata", {}).get("char_count", 0),
                },
                duration_ms=int((time.time() - started) * 1000),
            )
            return self._ensure_fuente_url(result, parsed)
        except Exception as e:
            logger.warning("FetchCorteCC failed for %s: %s", parsed.normalized, e)
            return self._build_error(e)
