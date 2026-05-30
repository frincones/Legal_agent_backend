"""Sprint M20.12 · Network proxy con allowlist real para sandbox.

Wrappea httpx.AsyncClient + bloquea requests fuera del allowlist.
Usado por código LLM cuando se ejecuta dentro del sandbox y necesita
acceder a fuentes oficiales gov.co (suin, corteconstitucional, etc.).

USO desde código sandboxed:
    import httpx
    from lex.sandbox import NetworkAllowlistProxy
    proxy = NetworkAllowlistProxy()
    async with proxy.client() as cli:
        resp = await cli.get("https://www.suin-juriscol.gov.co/...")  # OK
        resp = await cli.get("https://evil.com/...")  # BlockedHostError
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


DEFAULT_ALLOWED_HOSTS = frozenset([
    "suin-juriscol.gov.co",
    "www.suin-juriscol.gov.co",
    "corteconstitucional.gov.co",
    "www.corteconstitucional.gov.co",
    "cortesuprema.gov.co",
    "www.cortesuprema.gov.co",
    "secretariasenado.gov.co",
    "www.secretariasenado.gov.co",
    "funcionpublica.gov.co",
    "www.funcionpublica.gov.co",
    "datos.gov.co",
    "www.datos.gov.co",
    "banrep.gov.co",
    "www.banrep.gov.co",
    "dane.gov.co",
    "www.dane.gov.co",
])


class BlockedHostError(Exception):
    """Levantada cuando una request va a un host fuera del allowlist."""


def is_host_allowed(url: str, allowed: frozenset[str] = DEFAULT_ALLOWED_HOSTS) -> bool:
    """True si el host del URL está en allowlist."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        if host in allowed:
            return True
        # subdomain wildcard (e.g., relatoria.corteconstitucional.gov.co)
        for ah in allowed:
            if host.endswith("." + ah) or host == ah:
                return True
        return False
    except Exception:
        return False


class NetworkAllowlistProxy:
    """Wrappea httpx.AsyncClient añadiendo allowlist enforcement."""

    def __init__(self, allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_HOSTS,
                 timeout: float = 20.0):
        self.allowed = allowed_hosts
        self.timeout = timeout
        self.audit_log: list[dict] = []

    @asynccontextmanager
    async def client(self) -> AsyncIterator[Any]:
        try:
            import httpx
        except ImportError:
            raise RuntimeError("httpx required for NetworkAllowlistProxy")

        proxy = self
        original_request = httpx.AsyncClient.request

        async def gated_request(client_self, method, url, *args, **kwargs):
            url_str = str(url)
            if not is_host_allowed(url_str, proxy.allowed):
                logger.warning("BLOCKED network request: %s %s", method, url_str)
                proxy.audit_log.append({
                    "method": method, "url": url_str, "blocked": True,
                })
                raise BlockedHostError(
                    f"host fuera del allowlist: {url_str}. "
                    f"Permitidos: {sorted(proxy.allowed)[:5]}..."
                )
            proxy.audit_log.append({"method": method, "url": url_str, "blocked": False})
            return await original_request(client_self, method, url, *args, **kwargs)

        # Monkey-patch temporal (este client específico)
        httpx.AsyncClient.request = gated_request   # type: ignore
        client = httpx.AsyncClient(timeout=self.timeout)
        try:
            yield client
        finally:
            await client.aclose()
            httpx.AsyncClient.request = original_request   # type: ignore

    def get_audit_log(self) -> list[dict]:
        return list(self.audit_log)
