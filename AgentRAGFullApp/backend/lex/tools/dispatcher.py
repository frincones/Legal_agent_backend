"""Sprint M20.02 · ToolDispatcher · ejecuta tools y persiste audit.

Responsabilidades:
  - validar input contra input_schema (best-effort)
  - aplicar timeout
  - capturar excepciones y normalizarlas en ToolResult
  - hashear input/output (sha256) para deduplicación cache futura
  - persistir tool_call_audit (M20.01 tabla)
  - retornar ToolResult al Brain
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import traceback
from typing import Any, Optional

from .base import ToolCall, ToolContext, ToolDef, ToolError, ToolResult
from .registry import ToolRegistry

logger = logging.getLogger(__name__)


def _hash_payload(obj: Any) -> str:
    try:
        ser = json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False)
    except Exception:
        ser = repr(obj)
    return hashlib.sha256(ser.encode("utf-8")).hexdigest()[:16]


class ToolDispatcher:
    """Ejecuta una ToolCall y retorna ToolResult con audit."""

    def __init__(self, persist_audit: bool = True):
        self.persist_audit = persist_audit

    async def execute(
        self,
        call: ToolCall,
        registry: ToolRegistry,
        ctx: ToolContext,
    ) -> ToolResult:
        tool = registry.get(call.tool_name)
        if tool is None:
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                status="error",
                error_class="ToolNotFound",
                error_message=f"tool {call.tool_name!r} no está registrada",
                duration_ms=0,
            )

        started = time.perf_counter()
        in_hash = _hash_payload(call.input)
        result = ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            status="error",
        )

        try:
            output = await asyncio.wait_for(
                tool.run(ctx, **call.input),
                timeout=tool.timeout_seconds,
            )
            result.status = "success"
            result.output = output
        except asyncio.TimeoutError:
            result.status = "timeout"
            result.error_class = "TimeoutError"
            result.error_message = f"tool {call.tool_name!r} timeout {tool.timeout_seconds}s"
            logger.warning(result.error_message)
        except ToolError as e:
            result.status = "error"
            result.error_class = "ToolError"
            result.error_message = str(e)[:500]
            logger.info("tool %s controlled error: %s", call.tool_name, e)
        except Exception as e:
            result.status = "error"
            result.error_class = type(e).__name__
            result.error_message = str(e)[:500]
            logger.exception("tool %s unexpected error", call.tool_name)
            ctx.metadata.setdefault("errors", []).append({
                "tool": call.tool_name,
                "trace": traceback.format_exc()[-500:],
            })

        result.duration_ms = int((time.perf_counter() - started) * 1000)
        result.metadata["input_hash"] = in_hash
        if result.output is not None:
            result.metadata["output_hash"] = _hash_payload(result.output)

        # persist audit (best-effort, no rompe si BD falla)
        if self.persist_audit and ctx.pool is not None:
            try:
                await self._persist(call, result, ctx)
            except Exception as e:
                logger.warning("tool_call_audit persist failed: %s", e)

        return result

    async def execute_parallel(
        self,
        calls: list[ToolCall],
        registry: ToolRegistry,
        ctx: ToolContext,
        max_concurrent: int = 10,
    ) -> list[ToolResult]:
        """Ejecuta múltiples tools en paralelo (asyncio.gather con semáforo)."""
        sem = asyncio.Semaphore(max_concurrent)

        async def _one(call: ToolCall) -> ToolResult:
            async with sem:
                return await self.execute(call, registry, ctx)

        return await asyncio.gather(*[_one(c) for c in calls])

    async def _persist(self, call: ToolCall, result: ToolResult, ctx: ToolContext) -> None:
        """INSERT en tool_call_audit (tabla M20.01)."""
        sql = """
            insert into tool_call_audit (
              generation_id, firm_id, user_id, tool_name, iteration,
              started_at, duration_ms, input_hash, output_hash,
              success, error_class, error_message, cached,
              model_used, tokens_in, tokens_out, cost_usd,
              cache_creation_tokens, cache_read_tokens, metadata
            ) values (
              $1,$2,$3,$4,$5, now() - ($6 || ' milliseconds')::interval, $6,
              $7,$8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19
            )
        """
        success_val: Optional[bool]
        if result.status in ("success", "cached"):
            success_val = True
        elif result.status in ("error", "timeout"):
            success_val = False
        else:
            success_val = None

        async with ctx.pool.acquire() as conn:
            await conn.execute(
                sql,
                ctx.generation_id, ctx.firm_id, ctx.user_id,
                call.tool_name, call.iteration,
                result.duration_ms,
                result.metadata.get("input_hash"),
                result.metadata.get("output_hash"),
                success_val,
                result.error_class, result.error_message, result.cached,
                result.model_used, result.tokens_in, result.tokens_out, result.cost_usd,
                result.cache_creation_tokens, result.cache_read_tokens,
                json.dumps({k: v for k, v in result.metadata.items()
                            if k not in ("input_hash", "output_hash")},
                           default=str, ensure_ascii=False),
            )
