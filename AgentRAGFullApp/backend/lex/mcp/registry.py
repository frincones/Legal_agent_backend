"""Sprint M20.09 · Registry de los 6 MCP clients."""
from __future__ import annotations

from typing import Optional

from .base import BaseMCPClient


_REGISTRY: dict[str, BaseMCPClient] = {}


def register_mcp_client(name: str, client: BaseMCPClient) -> None:
    _REGISTRY[name] = client


def get_mcp_client(name: str) -> Optional[BaseMCPClient]:
    """Lazy init: instancia el client si no está, vía import dinámico."""
    if name in _REGISTRY:
        return _REGISTRY[name]

    try:
        if name == "corte_cc":
            from .corte_cc import CorteCcClient
            _REGISTRY[name] = CorteCcClient()
        elif name == "csj":
            from .csj import CsjClient
            _REGISTRY[name] = CsjClient()
        elif name == "suin":
            from .suin import SuinClient
            _REGISTRY[name] = SuinClient()
        elif name == "senado":
            from .senado import SenadoClient
            _REGISTRY[name] = SenadoClient()
        elif name == "funcpub":
            from .funcpub import FuncpubClient
            _REGISTRY[name] = FuncpubClient()
        elif name == "datosgov":
            from .datosgov import DatosgovClient
            _REGISTRY[name] = DatosgovClient()
        else:
            return None
    except Exception:
        return None
    return _REGISTRY.get(name)


def available_servers() -> list[str]:
    return ["corte_cc", "csj", "suin", "senado", "funcpub", "datosgov"]


def init_all_clients() -> dict[str, BaseMCPClient]:
    """Inicializa los 6 clients. Útil para el LeanOrchestrator startup."""
    clients = {}
    for name in available_servers():
        c = get_mcp_client(name)
        if c is not None:
            clients[name] = c
    return clients
