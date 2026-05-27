"""ToolDispatcher — decide qué tools invocar según parsed.kind + parsed.tipo.

Lógica determinística (NO LLM), basada en el análisis arquitectónico:
- Las fuentes son URL-predictable o APIs estructuradas
- No hay ambigüedad semántica que un LLM deba resolver
- Costo $0 + latencia mínima
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from lex.verify.tools.base import BaseTool, ToolResult
from lex.verify.tools.search_internal_db import SearchInternalDB
from lex.verify.tools.fetch_corte_cc import FetchCorteCC
from lex.verify.tools.fetch_csj_rss import FetchCSJRss
from lex.verify.tools.fetch_senado_suin import FetchSenadoSuin
from lex.verify.tools.lookup_articulo_chunks import LookupArticuloChunks
from lex.verify.tools.check_derogation import CheckDerogation
from lex.verify.tools.smart_search import SmartSearchTool
from lex.verify.tools.legal_data_hunter import LegalDataHunterTool

logger = logging.getLogger(__name__)

# Tipos de jurisprudencia por corte (para dispatcher)
CC_TIPOS = ("T", "C", "SU", "A")
CSJ_TIPOS = ("SL", "SC", "SP", "STC", "STL", "STP")
CE_TIPOS = ("CE",)


class ToolDispatcher:
    """Selecciona qué tools invocar y los ejecuta en paralelo."""

    def __init__(self, pool, client=None, max_concurrent: int = 4):
        self.pool = pool
        self.client = client
        self.semaphore = asyncio.Semaphore(max_concurrent)
        # Instanciar tools (compartidas entre verificaciones para reuso de connection pool)
        self._tools_cache: dict[str, BaseTool] = {}

    def _get_tool(self, tool_cls: type[BaseTool]) -> BaseTool:
        name = tool_cls.__name__
        if name not in self._tools_cache:
            self._tools_cache[name] = tool_cls(pool=self.pool, client=self.client)
        return self._tools_cache[name]

    def dispatch(self, parsed) -> list[BaseTool]:
        """Decide qué tools invocar según el kind + tipo de la cita.

        Estrategia M18:
        - SIEMPRE incluir SearchInternalDB (cache BD)
        - SIEMPRE incluir SmartSearchTool (norma_url_index lookup + Brave fallback)
        - Live fetch a fuente oficial específica según corte
        - check_derogation para normativas (no jurisprudencia)
        """
        if parsed.kind == "jurisprudencia":
            tools = [
                self._get_tool(LegalDataHunterTool),  # M19.4: catálogo interno (BD jurisprudencia)
                self._get_tool(SearchInternalDB),
                self._get_tool(SmartSearchTool),  # M18: índice + Brave para descubrir URL real
            ]
            if parsed.tipo in CC_TIPOS:
                tools.append(self._get_tool(FetchCorteCC))
            elif parsed.tipo in CSJ_TIPOS:
                tools.append(self._get_tool(FetchCSJRss))
            elif parsed.tipo in CE_TIPOS:
                # Consejo de Estado: por ahora reusar CSJ RSS (placeholder)
                tools.append(self._get_tool(FetchCSJRss))
            return tools

        if parsed.kind in ("ley", "decreto"):
            return [
                self._get_tool(LegalDataHunterTool),  # M19.4: BD leyes_normas
                self._get_tool(SearchInternalDB),
                self._get_tool(SmartSearchTool),  # M18: descubre URL Función Pública/SUIN
                self._get_tool(FetchSenadoSuin),
                self._get_tool(CheckDerogation),
            ]

        if parsed.kind == "codigo_articulo":
            return [
                self._get_tool(LegalDataHunterTool),  # M19.4: chunks corpus
                self._get_tool(LookupArticuloChunks),
                self._get_tool(SmartSearchTool),  # M18: URL real del código
                self._get_tool(SearchInternalDB),
            ]

        if parsed.kind == "codigo":
            return [
                self._get_tool(LegalDataHunterTool),  # M19.4: BD codigos
                self._get_tool(SearchInternalDB),
                self._get_tool(SmartSearchTool),  # M18: URL real del código
                self._get_tool(CheckDerogation),
            ]

        # Default: BD interna + SmartSearchTool como descubridor genérico
        return [
            self._get_tool(SearchInternalDB),
            self._get_tool(SmartSearchTool),
        ]

    async def execute_all(self, parsed, tools: list[BaseTool], on_tool=None) -> list[ToolResult]:
        """Ejecuta tools en paralelo con semáforo limitante.

        Args:
            on_tool: callback opcional async(tool_name, tool_id, status, request, response, error, duration_ms)
                     llamado dos veces por tool: status="running" antes, status="done"|"error" después.
        """
        import uuid as _uuid
        import time as _time

        async def _run_with_sem(tool: BaseTool) -> ToolResult:
            tool_id = _uuid.uuid4().hex[:12]
            started = _time.time()

            # Request preview = ParsedCitation summary
            req_preview = {
                "kind": getattr(parsed, "kind", None),
                "tipo": getattr(parsed, "tipo", None),
                "numero": getattr(parsed, "numero", None),
                "anio": getattr(parsed, "anio", None),
                "normalized": getattr(parsed, "normalized", None),
            }

            # Emit "running"
            if on_tool:
                try:
                    await on_tool(
                        tool_name=tool.name, tool_id=tool_id,
                        status="running", request=req_preview,
                        response=None, error=None, duration_ms=None,
                    )
                except Exception:
                    pass

            async with self.semaphore:
                try:
                    result = await asyncio.wait_for(
                        tool.run(parsed),
                        timeout=tool.timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    duration = int((_time.time() - started) * 1000)
                    if on_tool:
                        try:
                            await on_tool(
                                tool_name=tool.name, tool_id=tool_id,
                                status="error", request=req_preview,
                                response=None, error=f"timeout {tool.timeout_seconds}s",
                                duration_ms=duration,
                            )
                        except Exception:
                            pass
                    return ToolResult(
                        tool_name=tool.name, status="timeout", confidence=0.0,
                        error_message=f"timeout {tool.timeout_seconds}s",
                    )
                except Exception as e:
                    duration = int((_time.time() - started) * 1000)
                    logger.warning("tool %s error: %s", tool.name, e)
                    if on_tool:
                        try:
                            await on_tool(
                                tool_name=tool.name, tool_id=tool_id,
                                status="error", request=req_preview,
                                response=None, error=str(e)[:200],
                                duration_ms=duration,
                            )
                        except Exception:
                            pass
                    return ToolResult(
                        tool_name=tool.name, status="error", confidence=0.0,
                        error_message=str(e)[:200],
                    )

            # Emit "done" con response summary
            if on_tool:
                duration = int((_time.time() - started) * 1000)
                resp_preview = {
                    "status": result.status,
                    "confidence": result.confidence,
                    "fuente_url": result.fuente_url,
                    "titulo": (result.titulo or "")[:200] if result.titulo else None,
                    "snippet": (result.snippet or "")[:300] if result.snippet else None,
                    "discovered_by": result.discovered_by,
                }
                try:
                    await on_tool(
                        tool_name=tool.name, tool_id=tool_id,
                        status="done", request=req_preview,
                        response=resp_preview, error=None,
                        duration_ms=duration,
                    )
                except Exception:
                    pass

            return result

        return await asyncio.gather(*[_run_with_sem(t) for t in tools])
