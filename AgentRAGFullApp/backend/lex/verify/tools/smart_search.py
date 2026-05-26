"""Sprint M18 · SmartSearchTool — descubre URLs canónicas vía Brave Search.

Reemplaza la necesidad de hardcodear IDs en `citation_url_builder.py` para
la long-tail de normas (leyes/decretos/resoluciones no mapeadas).

Flujo:
  1. Lookup norma_url_index (cache compartido entre firms)
  2. Si miss: Brave Search restringido a gov.co
  3. Para top-3 resultados .gov.co: validate_url_responsive (GET + soft 404)
  4. Primer URL validado → ToolResult(hit, confidence=0.92)
  5. Persistir en norma_url_index para próximos uses
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from lex.verify.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class SmartSearchTool(BaseTool):
    """Descubre URL canónica vía cache + Brave Search API."""

    name = "smart_search"
    timeout_seconds = 12.0

    async def run(self, parsed) -> ToolResult:
        started = time.time()

        # Stage 1: Lookup en cache compartido norma_url_index
        try:
            from utils.norma_url_index import lookup_norma_url
            indexed = await lookup_norma_url(parsed, self.pool)
            if indexed and indexed.fuente_url:
                result = ToolResult(
                    tool_name=self.name,
                    status="hit",
                    confidence=max(indexed.confidence, 0.93),
                    fuente_url=indexed.fuente_url,
                    titulo=indexed.titulo,
                    raw_evidence={
                        "discovered_by": indexed.discovered_by,
                        "snippet": indexed.snippet,
                        "url_validated": indexed.url_validated,
                        "http_status": indexed.url_http_status,
                        "vigencia": indexed.vigencia,
                        "query_used": indexed.query_used,
                        "from_cache": True,
                    },
                    duration_ms=int((time.time() - started) * 1000),
                )
                return result
        except Exception as e:
            logger.debug("norma_url_index lookup failed (continuing): %s", e)

        # Stage 2: Brave Search
        try:
            from legal_sources.brave_search import search as brave_search, filter_govco, is_available
            if not is_available():
                return ToolResult(
                    tool_name=self.name,
                    status="miss",
                    confidence=0.0,
                    raw_evidence={"reason": "brave_api_key_not_configured"},
                    duration_ms=int((time.time() - started) * 1000),
                )

            # Construir query desde parsed
            query = self._build_query(parsed)
            if not query:
                return ToolResult(
                    tool_name=self.name,
                    status="miss",
                    confidence=0.0,
                    raw_evidence={"reason": "cannot_build_query"},
                    duration_ms=int((time.time() - started) * 1000),
                )

            brave_resp = await brave_search(query, count=5, restrict_govco=True)
            if not brave_resp.ok:
                return ToolResult(
                    tool_name=self.name,
                    status="miss",
                    confidence=0.0,
                    raw_evidence={
                        "reason": f"brave_error:{brave_resp.error}",
                        "query": query,
                    },
                    duration_ms=int((time.time() - started) * 1000),
                )

            # Filtrar solo gov.co
            govco_results = filter_govco(brave_resp.results)
            if not govco_results:
                return ToolResult(
                    tool_name=self.name,
                    status="miss",
                    confidence=0.0,
                    raw_evidence={
                        "reason": "no_govco_results",
                        "query": query,
                        "total_results": len(brave_resp.results),
                    },
                    duration_ms=int((time.time() - started) * 1000),
                )

            # Stage 3: validar top-3 con GET + soft 404
            from utils.url_validator import validate_url_responsive
            for candidate in govco_results[:3]:
                is_valid, http_status = await validate_url_responsive(
                    candidate.url, self.pool
                )
                if is_valid:
                    # Stage 4: persistir en índice
                    try:
                        from utils.norma_url_index import persist_norma_url
                        await persist_norma_url(
                            parsed=parsed,
                            pool=self.pool,
                            fuente_url=candidate.url,
                            discovered_by="brave_search",
                            titulo=candidate.title,
                            snippet=candidate.description,
                            url_validated=True,
                            url_http_status=http_status,
                            confidence=0.92,
                            query_used=query,
                            revalidate_days=7,
                        )
                    except Exception as e:
                        logger.debug("persist norma_url_index failed: %s", e)

                    return ToolResult(
                        tool_name=self.name,
                        status="hit",
                        confidence=0.92,
                        fuente_url=candidate.url,
                        titulo=candidate.title,
                        raw_evidence={
                            "discovered_by": "brave_search",
                            "snippet": candidate.description,
                            "query": query,
                            "result_rank": candidate.rank,
                            "total_results": len(brave_resp.results),
                            "url_validated": True,
                            "http_status": http_status,
                            "from_cache": False,
                        },
                        duration_ms=int((time.time() - started) * 1000),
                    )

            # Ningún candidato validó → miss
            return ToolResult(
                tool_name=self.name,
                status="miss",
                confidence=0.0,
                raw_evidence={
                    "reason": "no_validated_url",
                    "query": query,
                    "tried_count": min(3, len(govco_results)),
                },
                duration_ms=int((time.time() - started) * 1000),
            )

        except Exception as e:
            logger.warning("SmartSearchTool failed for %s: %s", getattr(parsed, "normalized", "?"), e)
            return self._build_error(e)

    def _build_query(self, parsed) -> Optional[str]:
        """Construye query Brave optimizada según kind/tipo."""
        kind = parsed.kind
        tipo = parsed.tipo
        numero = parsed.numero
        anio = parsed.anio

        # CONSTITUCION (no necesita Brave - patron predecible - pero ok)
        if tipo == "CONSTITUCION":
            if kind == "codigo_articulo" and numero:
                return f"Constitución Política Colombia 1991 artículo {numero}"
            return "Constitución Política Colombia 1991"

        # CST / Código X / Estatuto Y
        if kind in ("codigo", "codigo_articulo"):
            code_label = {
                "CST": "Código Sustantivo del Trabajo",
                "C.P.": "Código Penal Colombia",
                "C.C.": "Código Civil Colombia",
                "C.CO.": "Código de Comercio Colombia",
                "CGP": "Código General del Proceso",
                "CPACA": "CPACA Ley 1437 2011",
                "CPP": "Código Procedimiento Penal Ley 906 2004",
                "CPTSS": "Código Procesal Trabajo Seguridad Social",
            }.get(tipo, tipo)
            if kind == "codigo_articulo" and numero:
                return f'"{code_label}" artículo {numero}'
            return code_label

        # Ley / Decreto / Resolución con numero+anio
        if kind == "ley" and numero and anio:
            return f"Ley {numero} de {anio}"
        if kind == "decreto" and numero and anio:
            return f"Decreto {numero} de {anio}"

        # Jurisprudencia
        if kind == "jurisprudencia" and tipo and numero:
            if anio:
                yy = str(anio)[-2:]
                # SU sin guión, T/C con guión
                if tipo == "SU":
                    return f"Sentencia {tipo}-{numero}/{yy} Corte Constitucional"
                if tipo in ("T", "C", "A"):
                    return f"Sentencia {tipo}-{numero} de {anio} Corte Constitucional"
                if tipo in ("SL", "SC", "SP", "STC", "STL", "STP"):
                    return f"Sentencia {tipo}{numero} {anio} Corte Suprema"
            return f"{tipo}{numero} jurisprudencia Colombia"

        # Fallback genérico
        return getattr(parsed, "normalized", None)
