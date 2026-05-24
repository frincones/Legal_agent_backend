"""Tool: CheckDerogation — wraps DerogationVerifier.check()."""
from __future__ import annotations

import logging
import time

from lex.verify.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class CheckDerogation(BaseTool):
    """Verifica vigencia normativa contra tabla derogaciones."""

    name = "check_derogation"
    timeout_seconds = 3.0

    async def run(self, parsed) -> ToolResult:
        started = time.time()
        if parsed.kind not in ("ley", "decreto", "codigo", "codigo_articulo"):
            return self._build_miss(f"kind_not_normativo:{parsed.kind}")

        try:
            from lex.verify.derogation_verifier import DerogationVerifier
            v = DerogationVerifier(self.pool)
            d = await v.check(parsed.raw)
            return ToolResult(
                tool_name=self.name,
                status="hit" if d.vigente else "miss",
                confidence=d.confidence,
                raw_evidence={
                    "vigente": d.vigente,
                    "derogada_por": d.derogada_por,
                    "fecha_derogacion": d.fecha_derogacion,
                },
                duration_ms=int((time.time() - started) * 1000),
            )
        except Exception as e:
            logger.warning("CheckDerogation failed: %s", e)
            return self._build_error(e)
