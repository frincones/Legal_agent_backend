"""Sprint M20.09 · 6 MCP servers Colombia (Corte CC, CSJ, SUIN, Senado, FuncPub, datos.gov).

Cada server expone tools específicas vía JSON-RPC sobre HTTP, conectándose
a fuentes oficiales colombianas. Reemplazan/complementan a Westlaw/CourtListener
de Claude for Legal (que no cubren CO).

Stack:
  - httpx (async HTTP client)
  - beautifulsoup4 (HTML parsing)
  - cachetools (in-process TTL cache)
  - rate limiter respetuoso (10 req/min default)

Modo de operación:
  - In-process: instanciar `CorteCcClient()` directamente desde el backend
  - Standalone: `python -m lex.mcp.standalone --server corte_cc --port 3001`
    (cuando se quieran deployar como sidecars en Railway)

USO desde el LeanOrchestrator:
    from lex.mcp.registry import get_mcp_client
    client = get_mcp_client("corte_cc")
    result = await client.call("fetch_sentencia", {"tipo": "T", "numero": 67, "año": 2025})
"""

from .base import BaseMCPClient, MCPCall, MCPResult, RateLimiter
from .registry import get_mcp_client, register_mcp_client, available_servers

__all__ = [
    "BaseMCPClient",
    "MCPCall",
    "MCPResult",
    "RateLimiter",
    "get_mcp_client",
    "register_mcp_client",
    "available_servers",
]
