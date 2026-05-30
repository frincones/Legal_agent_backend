"""Sprint M20.09 · Base MCP client + rate limiter + cache."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class MCPCall:
    method: str
    params: dict = field(default_factory=dict)


@dataclass
class MCPResult:
    success: bool
    data: Any = None
    error: Optional[str] = None
    source_url: Optional[str] = None
    cached: bool = False
    duration_ms: int = 0


class RateLimiter:
    """Rate limiter simple por server (max N requests por ventana)."""

    def __init__(self, max_per_minute: int = 10):
        self.max_per_minute = max_per_minute
        self._timestamps: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.time()
            self._timestamps = [t for t in self._timestamps if now - t < 60]
            if len(self._timestamps) >= self.max_per_minute:
                wait = 60 - (now - self._timestamps[0])
                logger.info("Rate limit hit, esperando %.1fs", wait)
                await asyncio.sleep(max(0.1, wait))
                self._timestamps = [t for t in self._timestamps if time.time() - t < 60]
            self._timestamps.append(time.time())


class _LocalCache:
    """Cache in-process con TTL por entrada."""

    def __init__(self, max_entries: int = 200):
        self._data: dict[str, tuple[float, Any]] = {}
        self._max = max_entries

    def get(self, key: str) -> Optional[Any]:
        entry = self._data.get(key)
        if not entry:
            return None
        expires, val = entry
        if expires < time.time():
            self._data.pop(key, None)
            return None
        return val

    def set(self, key: str, val: Any, ttl_s: int) -> None:
        if len(self._data) >= self._max:
            try:
                oldest = next(iter(self._data))
                self._data.pop(oldest, None)
            except Exception:
                pass
        self._data[key] = (time.time() + ttl_s, val)


class BaseMCPClient:
    """Cliente base para los 6 MCP CO. Subclases implementan los métodos específicos."""

    server_name: str = "base"
    user_agent: str = "LexAI/2.0 (legal-research; contact@lexai.co)"
    max_per_minute: int = 10
    default_cache_ttl_s: int = 86400   # 24h default

    def __init__(self, http_client=None):
        self.http = http_client
        self._rate_limit = RateLimiter(self.max_per_minute)
        self._cache = _LocalCache()
        self.methods: dict[str, callable] = self._register_methods()

    def _register_methods(self) -> dict:
        """Subclases override para registrar sus métodos."""
        return {}

    async def call(self, method: str, params: Optional[dict] = None) -> MCPResult:
        """Punto de entrada principal. Aplica rate limit + cache + dispatch."""
        params = params or {}
        started = time.perf_counter()

        if method not in self.methods:
            return MCPResult(
                success=False,
                error=f"método {method!r} no soportado en server {self.server_name!r}. "
                      f"Disponibles: {sorted(self.methods.keys())}",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        cache_key = self._make_cache_key(method, params)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return MCPResult(
                success=True, data=cached, cached=True,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        await self._rate_limit.acquire()
        try:
            handler = self.methods[method]
            result = await handler(**params)
            self._cache.set(cache_key, result, self.default_cache_ttl_s)
            return MCPResult(
                success=True, data=result,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as e:
            logger.warning("%s.%s falló: %s", self.server_name, method, e)
            return MCPResult(
                success=False, error=f"{type(e).__name__}: {str(e)[:300]}",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

    def _make_cache_key(self, method: str, params: dict) -> str:
        import json
        canon = json.dumps(params, sort_keys=True, default=str, ensure_ascii=False)
        h = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
        return f"{self.server_name}:{method}:{h}"

    async def _http_get(self, url: str, **kwargs) -> str:
        """Helper HTTP GET con User-Agent + timeout."""
        try:
            import httpx
        except ImportError:
            raise RuntimeError("httpx required. pip install httpx")

        client = self.http or httpx.AsyncClient(timeout=20.0)
        try:
            resp = await client.get(
                url, headers={"User-Agent": self.user_agent}, **kwargs,
            )
            resp.raise_for_status()
            return resp.text
        finally:
            if self.http is None:
                await client.aclose()
