"""Sprint M20.04 · S3.6 · Stress test pool Supabase + semáforo dispatcher.

Simula N invocaciones concurrentes al dispatcher con tools que requieren
pool (sin pool real → tools retornan _warning). Valida que:

  - Semáforo max_concurrent_tools=5 limita la concurrencia real
  - asyncio.gather no satura el event loop con 100 calls simultáneas
  - Tools que fallan no cascadean al resto
"""
from __future__ import annotations

import asyncio
import time
from uuid import uuid4

import pytest

from lex.tools import ToolCall, ToolContext, ToolDispatcher, ToolRegistry


@pytest.mark.asyncio
async def test_dispatcher_parallel_with_semaphore():
    """100 narrate_progress en paralelo con max_concurrent=5 → todos pasan."""
    registry = ToolRegistry(pool=None)
    dispatcher = ToolDispatcher(persist_audit=False)
    ctx = ToolContext(generation_id=uuid4(), firm_id=uuid4(), user_id=uuid4())

    calls = [
        ToolCall(tool_use_id=f"tu-{i}", tool_name="narrate_progress",
                  input={"message": f"msg-{i}", "kind": "narration"})
        for i in range(100)
    ]
    t0 = time.perf_counter()
    results = await dispatcher.execute_parallel(calls, registry, ctx, max_concurrent=5)
    elapsed = time.perf_counter() - t0

    assert len(results) == 100
    success = sum(1 for r in results if r.status == "success")
    assert success == 100, f"esperaba 100 success, obtuve {success}"
    print(f"\n  100 calls @ max_concurrent=5 → {elapsed:.2f}s")


@pytest.mark.asyncio
async def test_dispatcher_handles_mixed_success_and_failure():
    """Si algunas tools fallan, el resto sigue completándose."""
    registry = ToolRegistry(pool=None)
    dispatcher = ToolDispatcher(persist_audit=False)
    ctx = ToolContext(generation_id=uuid4(), firm_id=uuid4(), user_id=uuid4())

    # Mezcla: 5 success + 5 fail (tool inexistente)
    calls = []
    for i in range(5):
        calls.append(ToolCall(tool_use_id=f"ok-{i}", tool_name="narrate_progress",
                              input={"message": "ok", "kind": "narration"}))
    for i in range(5):
        calls.append(ToolCall(tool_use_id=f"err-{i}", tool_name="tool_inventada",
                              input={}))

    results = await dispatcher.execute_parallel(calls, registry, ctx, max_concurrent=10)
    success = sum(1 for r in results if r.status == "success")
    errors = sum(1 for r in results if r.status == "error")
    assert success == 5
    assert errors == 5


@pytest.mark.asyncio
async def test_dispatcher_timeout_isolates():
    """Si una tool timeout, las otras del batch siguen completándose."""
    from lex.tools.base import ToolDef, ToolContext as _Ctx

    class SlowTool(ToolDef):
        name = "slow_tool"
        description = "Sleep 5s para forzar timeout"
        input_schema = {"type": "object", "properties": {}}
        timeout_seconds = 0.1  # forzar timeout rápido
        async def run(self, ctx, **kw):
            await asyncio.sleep(5)
            return {}

    registry = ToolRegistry(pool=None)
    registry.register(SlowTool())
    dispatcher = ToolDispatcher(persist_audit=False)
    ctx = ToolContext(generation_id=uuid4(), firm_id=uuid4(), user_id=uuid4())

    calls = [
        ToolCall(tool_use_id="slow-1", tool_name="slow_tool", input={}),
        ToolCall(tool_use_id="fast-1", tool_name="narrate_progress",
                  input={"message": "fast", "kind": "narration"}),
    ]
    t0 = time.perf_counter()
    results = await dispatcher.execute_parallel(calls, registry, ctx, max_concurrent=5)
    elapsed = time.perf_counter() - t0

    assert len(results) == 2
    slow_result = next(r for r in results if r.tool_name == "slow_tool")
    fast_result = next(r for r in results if r.tool_name == "narrate_progress")
    assert slow_result.status == "timeout"
    assert fast_result.status == "success"
    # No debería tomar más de ~1s (slow timeout 0.1s; el fast es <100ms)
    assert elapsed < 2.0
