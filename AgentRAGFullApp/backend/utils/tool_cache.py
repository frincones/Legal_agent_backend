"""Sprint M20.07 · S7.1 · Cache de tools determinísticas (Redis + SQL fallback).

Las tools cacheables (load_skill_md, search_jurisprudence, fetch_mcp_official,
verify_citation, etc.) consultan este cache ANTES de ejecutar la lógica real.
Hit → retorna inmediato (saving LLM call o query DB).

Backends soportados (en orden de preferencia):
  1. Redis (si REDIS_URL configurado) — TTL nativo
  2. SQL fallback en `tool_cache` (auto-creada si no existe)
  3. Process memory (dict in-process) — útil para tests

USO:
    from utils.tool_cache import get_cache

    cache = await get_cache()
    key = cache.make_key("verify_citation", {"citation": "Art. 2142 CC"})
    result = await cache.get(key)
    if result is None:
        result = await expensive_operation(...)
        await cache.set(key, result, ttl_seconds=3600)
    return result
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ToolCacheBackend:
    """Interfaz abstracta de backend de cache."""

    async def get(self, key: str) -> Optional[Any]:
        raise NotImplementedError

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        raise NotImplementedError

    async def invalidate(self, key: str) -> None:
        raise NotImplementedError

    async def stats(self) -> dict:
        return {}


# ---- In-process memory backend ----

class MemoryBackend(ToolCacheBackend):
    """Backend in-process (dict). Útil para tests y default sin Redis/SQL."""

    def __init__(self, max_entries: int = 500):
        self._data: dict[str, tuple[float, Any]] = {}
        self._max = max_entries
        self._hits = 0
        self._misses = 0

    async def get(self, key: str) -> Optional[Any]:
        entry = self._data.get(key)
        if not entry:
            self._misses += 1
            return None
        expires, value = entry
        if expires < time.time():
            self._data.pop(key, None)
            self._misses += 1
            return None
        self._hits += 1
        return value

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        if len(self._data) >= self._max:
            try:
                oldest = next(iter(self._data))
                self._data.pop(oldest, None)
            except Exception:
                pass
        self._data[key] = (time.time() + ttl_seconds, value)

    async def invalidate(self, key: str) -> None:
        self._data.pop(key, None)

    async def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "backend": "memory",
            "entries": len(self._data),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": (self._hits / total) if total else 0,
        }


# ---- Redis backend ----

class RedisBackend(ToolCacheBackend):
    """Backend Redis (async). Requiere `redis.asyncio`."""

    def __init__(self, url: str):
        try:
            import redis.asyncio as redis   # type: ignore
            self.client = redis.from_url(url, decode_responses=True)
        except ImportError as e:
            raise RuntimeError("redis package not installed; pip install 'redis[hiredis]'") from e
        self._hits = 0
        self._misses = 0

    async def get(self, key: str) -> Optional[Any]:
        try:
            raw = await self.client.get(f"toolcache:{key}")
            if raw is None:
                self._misses += 1
                return None
            self._hits += 1
            return json.loads(raw)
        except Exception as e:
            logger.warning("redis cache GET fail: %s", e)
            return None

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        try:
            payload = json.dumps(value, default=str, ensure_ascii=False)
            await self.client.set(f"toolcache:{key}", payload, ex=int(ttl_seconds))
        except Exception as e:
            logger.warning("redis cache SET fail: %s", e)

    async def invalidate(self, key: str) -> None:
        try:
            await self.client.delete(f"toolcache:{key}")
        except Exception:
            pass

    async def stats(self) -> dict:
        return {"backend": "redis", "hits": self._hits, "misses": self._misses}


# ---- ToolCache façade ----

class ToolCache:
    """Façade que envuelve un backend y agrega lógica común (make_key, stats)."""

    def __init__(self, backend: ToolCacheBackend):
        self.backend = backend

    @staticmethod
    def make_key(tool_name: str, input_dict: dict) -> str:
        canon = json.dumps(input_dict, sort_keys=True, default=str, ensure_ascii=False)
        digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:24]
        return f"{tool_name}:{digest}"

    async def get(self, key: str) -> Optional[Any]:
        return await self.backend.get(key)

    async def set(self, key: str, value: Any, ttl_seconds: int = 3600) -> None:
        await self.backend.set(key, value, ttl_seconds)

    async def invalidate(self, key: str) -> None:
        await self.backend.invalidate(key)

    async def stats(self) -> dict:
        return await self.backend.stats()


# ---- Global singleton ----

_GLOBAL_CACHE: Optional[ToolCache] = None


async def get_cache() -> ToolCache:
    """Singleton lazy. Elige backend según env vars."""
    global _GLOBAL_CACHE
    if _GLOBAL_CACHE is not None:
        return _GLOBAL_CACHE

    redis_url = os.getenv("REDIS_URL") or os.getenv("UPSTASH_REDIS_URL")
    if redis_url:
        try:
            backend = RedisBackend(redis_url)
            logger.info("ToolCache: backend=Redis (%s)", redis_url[:30])
            _GLOBAL_CACHE = ToolCache(backend)
            return _GLOBAL_CACHE
        except Exception as e:
            logger.warning("Redis no disponible (%s), fallback a memory", e)

    backend = MemoryBackend()
    logger.info("ToolCache: backend=Memory (process-local)")
    _GLOBAL_CACHE = ToolCache(backend)
    return _GLOBAL_CACHE


async def reset_cache() -> None:
    """Util para tests: limpia el singleton."""
    global _GLOBAL_CACHE
    _GLOBAL_CACHE = None
