"""Tool 9 · fetch_mcp_official · dispatcher a los 6 MCP servers Colombia.

S1.10: skeleton con fallback a Legal Data Hunter MCP existente.
S9   : reemplaza con los 6 servers reales (corte_cc, csj, suin, senado, funcpub, datosgov).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .base import ToolContext, ToolDef, ToolError

logger = logging.getLogger(__name__)


VALID_SERVERS = {"corte_cc", "csj", "suin", "senado", "funcpub", "datosgov", "legal_data_hunter"}


class FetchMcpOfficialTool(ToolDef):
    name = "fetch_mcp_official"
    description = (
        "★ USAR PROACTIVAMENTE cuando una cita NO esté en la whitelist del SKILL "
        "cargado, O cuando tengas duda sobre vigencia/aplicabilidad de una norma. "
        "Es PREFERIBLE 1 call extra (~10s) que generar cita errónea. "
        "Consulta directa a 6 MCP servers oficiales de Colombia: "
        "corte_cc (Corte Constitucional), csj (Corte Suprema), "
        "suin (SUIN-Juriscol normativa), senado (Sec. Senado leyes consolidadas), "
        "funcpub (Función Pública), datosgov (datos.gov.co). "
        "USAR también después de verify_citation con VERIFY_FLAG/NOT_FOUND, "
        "o cuando se necesite texto literal de una norma/sentencia conocida."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
                "enum": sorted(VALID_SERVERS),
            },
            "method": {"type": "string", "description": "Método del MCP server (e.g., fetch_sentencia, fetch_norma)"},
            "params": {"type": "object", "description": "Parámetros del método", "default": {}},
        },
        "required": ["server", "method"],
    }
    cacheable = True
    cache_ttl_seconds = 86400
    timeout_seconds = 30.0

    def __init__(self, mcp_clients: Optional[dict] = None, **_: Any):
        self.mcp_clients = mcp_clients or {}

    async def run(
        self,
        ctx: ToolContext,
        server: str,
        method: str,
        params: Optional[dict] = None,
    ) -> dict:
        params = params or {}
        if server not in VALID_SERVERS:
            raise ToolError(f"server {server!r} no soportado. Disponibles: {sorted(VALID_SERVERS)}")

        clients = self.mcp_clients or getattr(ctx, "mcp_clients", {}) or {}
        client = clients.get(server)

        # M20.09: usar los 6 MCP servers CO si están registrados
        if client is None and server in ("corte_cc", "csj", "suin", "senado", "funcpub", "datosgov"):
            try:
                from lex.mcp import get_mcp_client
                client = get_mcp_client(server)
            except Exception as e:
                logger.warning("get_mcp_client(%s) falló: %s", server, e)

        # Fallback S1: si no hay client específico, intentar legal_data_hunter como genérico
        if client is None and server != "legal_data_hunter":
            client = clients.get("legal_data_hunter")
            if client is not None:
                logger.info("fetch_mcp_official: %r no inicializado, fallback a legal_data_hunter", server)

        if client is None:
            return {
                "server": server,
                "method": method,
                "_warning": (
                    f"MCP server {server!r} no inicializado en este deploy."
                ),
                "params": params,
                "data": None,
            }

        try:
            # MCP CO clients exponen .call(method, params); legacy expone .call_tool
            if hasattr(client, "call"):
                from lex.mcp.base import MCPResult
                result = await client.call(method, params)
                if isinstance(result, MCPResult):
                    return {
                        "server": server, "method": method, "params": params,
                        "data": result.data, "cached": result.cached,
                        "duration_ms": result.duration_ms,
                        "success": result.success,
                        "error": result.error,
                    }
                return {"server": server, "method": method, "params": params, "data": result}
            result = await client.call_tool(method, params)
            return {"server": server, "method": method, "params": params, "data": result}
        except Exception as e:
            logger.warning("fetch_mcp_official %s/%s failed: %s", server, method, e)
            return {
                "server": server, "method": method, "params": params,
                "data": None,
                "_error": str(e)[:300],
            }


def build_tool(mcp_clients: Optional[dict] = None, **_: Any) -> ToolDef:
    return FetchMcpOfficialTool(mcp_clients=mcp_clients)
