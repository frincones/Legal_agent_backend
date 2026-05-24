"""Tool: SearchInternalDB — wraps utils/citation_verifier legacy.

Reusa _verify_jurisprudencia, _verify_ley_o_decreto, _verify_codigo y
_verify_codigo_articulo del módulo legacy. Esta tool unifica el dispatch
por kind para que el VerificationAgent tenga UNA tool de "BD interna".
"""
from __future__ import annotations

import logging
import time

from lex.verify.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class SearchInternalDB(BaseTool):
    """Lookup en BD jurisprudencia/leyes_normas via legacy verifier."""

    name = "search_internal_db"
    timeout_seconds = 5.0

    async def run(self, parsed) -> ToolResult:
        started = time.time()
        try:
            from utils.citation_verifier import (
                _verify_jurisprudencia,
                _verify_ley_o_decreto,
                _verify_codigo,
                _verify_codigo_articulo,
            )
            if parsed.kind == "jurisprudencia":
                vr = await _verify_jurisprudencia(self.pool, parsed, None, None)
            elif parsed.kind in ("ley", "decreto"):
                vr = await _verify_ley_o_decreto(self.pool, parsed, None, None)
            elif parsed.kind == "codigo":
                vr = await _verify_codigo(self.pool, parsed, None, None)
            elif parsed.kind == "codigo_articulo":
                vr = await _verify_codigo_articulo(self.pool, parsed, None, None)
            else:
                return self._build_miss(f"unknown_kind:{parsed.kind}")

            # Solo retornar HIT si fue cache/bd (no si fue live fetch)
            # Live fetch lo manejarán las otras tools individualmente para
            # mejor trazabilidad y confidence calibrado.
            if vr.estado in ("verificada", "superada") and vr.source in (
                "cache", "bd", "bd_codigo_known", "bd_norma_known",
                "codigo_articulo:bd_codigo_known", "codigo_articulo:cache"
            ):
                conf = 0.99 if vr.source == "cache" else 0.95
                result = ToolResult(
                    tool_name=self.name,
                    status="hit",
                    confidence=conf,
                    fuente_url=vr.fuente_url,
                    titulo=vr.titulo or vr.rubro,
                    chunk_id=vr.juris_id or vr.norma_id,
                    raw_evidence={
                        "estado": vr.estado,
                        "source": vr.source,
                        "vigencia": vr.vigencia,
                        "derogada": vr.estado == "superada",
                    },
                    duration_ms=int((time.time() - started) * 1000),
                )
                return self._ensure_fuente_url(result, parsed)
            return ToolResult(
                tool_name=self.name,
                status="miss",
                confidence=0.0,
                raw_evidence={"legacy_estado": vr.estado, "legacy_source": vr.source},
                duration_ms=int((time.time() - started) * 1000),
            )
        except Exception as e:
            logger.warning("SearchInternalDB failed for %s: %s", parsed.normalized, e)
            return self._build_error(e)
