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

        Estrategia:
        - SIEMPRE consultar BD interna primero (cache)
        - Live fetch a fuente oficial específica según corte
        - check_derogation para normativas (no jurisprudencia)
        """
        if parsed.kind == "jurisprudencia":
            tools = [self._get_tool(SearchInternalDB)]
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
                self._get_tool(SearchInternalDB),
                self._get_tool(FetchSenadoSuin),
                self._get_tool(CheckDerogation),
            ]

        if parsed.kind == "codigo_articulo":
            return [
                self._get_tool(LookupArticuloChunks),
                self._get_tool(SearchInternalDB),
            ]

        if parsed.kind == "codigo":
            return [
                self._get_tool(SearchInternalDB),
                self._get_tool(CheckDerogation),
            ]

        # Default: solo BD interna
        return [self._get_tool(SearchInternalDB)]

    async def execute_all(self, parsed, tools: list[BaseTool]) -> list[ToolResult]:
        """Ejecuta tools en paralelo con semáforo limitante."""

        async def _run_with_sem(tool: BaseTool) -> ToolResult:
            async with self.semaphore:
                try:
                    return await asyncio.wait_for(
                        tool.run(parsed),
                        timeout=tool.timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    return ToolResult(
                        tool_name=tool.name,
                        status="timeout",
                        confidence=0.0,
                        error_message=f"timeout {tool.timeout_seconds}s",
                    )
                except Exception as e:
                    logger.warning("tool %s error: %s", tool.name, e)
                    return ToolResult(
                        tool_name=tool.name,
                        status="error",
                        confidence=0.0,
                        error_message=str(e)[:200],
                    )

        return await asyncio.gather(*[_run_with_sem(t) for t in tools])
