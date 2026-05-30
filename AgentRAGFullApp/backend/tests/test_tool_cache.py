"""Sprint M20.07 · Tests del tool_cache (MemoryBackend)."""
from __future__ import annotations

import asyncio

import pytest

from utils.tool_cache import MemoryBackend, ToolCache, get_cache, reset_cache


@pytest.fixture(autouse=True)
def clean_cache():
    asyncio.run(reset_cache())
    yield
    asyncio.run(reset_cache())


@pytest.mark.asyncio
async def test_make_key_deterministic():
    k1 = ToolCache.make_key("verify_citation", {"citation": "Art. 1 CC"})
    k2 = ToolCache.make_key("verify_citation", {"citation": "Art. 1 CC"})
    assert k1 == k2


@pytest.mark.asyncio
async def test_make_key_differs_by_input():
    k1 = ToolCache.make_key("verify_citation", {"citation": "Art. 1 CC"})
    k2 = ToolCache.make_key("verify_citation", {"citation": "Art. 2 CC"})
    assert k1 != k2


@pytest.mark.asyncio
async def test_make_key_independent_of_dict_order():
    k1 = ToolCache.make_key("x", {"a": 1, "b": 2})
    k2 = ToolCache.make_key("x", {"b": 2, "a": 1})
    assert k1 == k2


@pytest.mark.asyncio
async def test_memory_backend_get_set():
    backend = MemoryBackend()
    cache = ToolCache(backend)
    key = cache.make_key("t", {"a": 1})
    assert await cache.get(key) is None
    await cache.set(key, {"result": "ok"}, ttl_seconds=60)
    assert await cache.get(key) == {"result": "ok"}


@pytest.mark.asyncio
async def test_memory_backend_ttl_expiration():
    backend = MemoryBackend()
    cache = ToolCache(backend)
    key = cache.make_key("t", {"a": 1})
    await cache.set(key, "value", ttl_seconds=0)
    await asyncio.sleep(0.01)
    assert await cache.get(key) is None


@pytest.mark.asyncio
async def test_memory_backend_invalidate():
    backend = MemoryBackend()
    cache = ToolCache(backend)
    key = cache.make_key("t", {"a": 1})
    await cache.set(key, "value", ttl_seconds=60)
    await cache.invalidate(key)
    assert await cache.get(key) is None


@pytest.mark.asyncio
async def test_memory_backend_evicts_oldest():
    backend = MemoryBackend(max_entries=3)
    cache = ToolCache(backend)
    for i in range(5):
        await cache.set(f"k{i}", i, ttl_seconds=60)
    # Solo deben quedar 3
    stats = await cache.stats()
    assert stats["entries"] == 3


@pytest.mark.asyncio
async def test_stats_hit_miss():
    backend = MemoryBackend()
    cache = ToolCache(backend)
    await cache.get("missing")   # miss
    await cache.set("k1", "v", 60)
    await cache.get("k1")        # hit
    await cache.get("k1")        # hit
    stats = await cache.stats()
    assert stats["hits"] == 2
    assert stats["misses"] == 1
    assert stats["hit_rate"] == 2 / 3


@pytest.mark.asyncio
async def test_global_singleton():
    c1 = await get_cache()
    c2 = await get_cache()
    assert c1 is c2
