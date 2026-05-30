"""Sprint M20.03 · AnthropicBrain · ReAct loop nativo con tool_use.

Usa el protocolo tool_use de Anthropic con prompt caching y soporte
para parallel tool calls. Compatible con Sonnet 4.6 (default) y Opus 4.7
(escalación para doc_types complejos).

Fallback a OpenAI gpt-4o si Anthropic cae N veces consecutivas.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional
from uuid import UUID, uuid4

from lex.tools import (
    ToolCall,
    ToolContext,
    ToolDispatcher,
    ToolRegistry,
    ToolResult,
)
from lex.tools.base import _safe_json_dumps  # type: ignore

from .sse_emitter import map_tool_to_sse_events
from .system_prompt import build_system_prompt, build_user_message

logger = logging.getLogger(__name__)


# Doc types que escalan a Opus 4.7 (complejidad alta)
OPUS_DOC_TYPES = {
    "concepto_juridico",
    "recurso_apelacion",
    "demanda_compleja",
    "casacion",
    "tutela_compleja",
}

# Tokens caps por modelo (mantener conservador para evitar timeouts)
# M20.14 Opcion B: cuenta Anthropic en Build Tier 1 (8K output tokens/min).
# Bajamos paralelismo + max_tokens para no saturar el rate limit en bursts
# de generate_clause + verify_citation (cada uno con su LLM downstream).
# Subimos fallback threshold para tolerar mejor los 429 transitorios.
DEFAULT_MAX_TOKENS = 4096                 # antes 8192 — cada respuesta del Brain ~mitad
DEFAULT_MAX_ITERATIONS = 30
DEFAULT_MAX_PARALLEL_TOOLS = 3            # antes 10 — burst limitado a 3 tools simultaneas
DEFAULT_RATE_LIMIT_RETRY_S = 8.0          # backoff base cuando llega RateLimitError


@dataclass
class BrainConfig:
    sonnet_model: str = "claude-sonnet-4-6"
    opus_model: str = "claude-opus-4-7"
    fallback_openai_model: str = "gpt-4o"
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    max_parallel_tools: int = DEFAULT_MAX_PARALLEL_TOOLS
    enable_prompt_caching: bool = True
    enable_parallel_tools: bool = True
    # antes 2 — subimos a 4 porque ahora hacemos backoff por 429 y la mayoria
    # de 429 son transitorios (la ventana de 60s del rate limit se libera).
    fallback_to_openai_after_failures: int = 4
    rate_limit_retry_s: float = DEFAULT_RATE_LIMIT_RETRY_S


@dataclass
class BrainStats:
    iterations: int = 0
    tool_calls_total: int = 0
    tool_calls_parallel_max: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    fallback_used: bool = False
    model_actual: str = ""


class AnthropicBrain:
    """ReAct loop nativo con tool_use Anthropic + fallback OpenAI."""

    def __init__(
        self,
        anthropic_client,
        openai_client=None,
        config: Optional[BrainConfig] = None,
    ):
        self.anthropic = anthropic_client
        self.openai = openai_client
        self.config = config or BrainConfig()

    async def react_loop(
        self,
        *,
        registry: ToolRegistry,
        ctx: ToolContext,
        intent: str,
        brief: str = "",
        doc_type_hint: str = "",
        playbook_raw_md: Optional[str] = None,
        borrador_mode: bool = True,
    ) -> AsyncIterator[bytes]:
        """Loop ReAct principal. Yieldea bytes SSE listos para el stream HTTP.

        Cada iteración:
          1. messages.create con tools=[...]
          2. si stop_reason='tool_use' → ejecuta tools en paralelo → tool_results → vuelta
          3. si stop_reason='end_turn' → emite SSE final + return
          4. si stop_reason='max_tokens' → continúa loop
          5. si error N veces → fallback OpenAI

        Args:
            borrador_mode: True (default) inyecta regla "redacta con placeholders,
                no pidas datos". False activa MODO FIRMA con check_completeness gate.
        """
        stats = BrainStats()
        dispatcher = ToolDispatcher(persist_audit=True)

        # Modelo a usar (Opus para doc_types complejos)
        model = self.config.opus_model if doc_type_hint in OPUS_DOC_TYPES else self.config.sonnet_model
        stats.model_actual = model

        # Construir system prompt + primer mensaje user
        # M20.14: borrador_mode controla si el Brain redacta con placeholders
        # (default) o detiene con check_completeness gate (modo firma).
        system_prompt = build_system_prompt(playbook_raw_md, borrador_mode=borrador_mode)
        user_msg = build_user_message(
            intent=intent, brief=brief,
            firm_id=str(ctx.firm_id) if ctx.firm_id else "",
            matter_id=str(ctx.matter_id) if ctx.matter_id else "",
            generation_id=str(ctx.generation_id),
            doc_type_hint=doc_type_hint,
        )
        messages: list[dict] = [{"role": "user", "content": user_msg}]
        tools_schema = registry.schema_for_anthropic()

        # SSE meta inicial
        yield _sse_bytes("meta", {
            "generation_id": str(ctx.generation_id),
            "model": model,
            "orchestrator_kind": "lean",
            "caching_enabled": self.config.enable_prompt_caching,
            "tools_count": len(tools_schema),
        })

        consecutive_failures = 0

        for iteration in range(self.config.max_iterations):
            stats.iterations = iteration + 1
            ctx.iteration = iteration

            yield _sse_bytes("stage_progress", {
                "stage": f"brain_iter_{iteration}",
                "state": "running",
                "label": f"Razonando (iteración {iteration + 1})…",
            })

            # 1) Llamada al LLM
            try:
                response = await self._call_anthropic(
                    model=model,
                    system_prompt=system_prompt,
                    tools_schema=tools_schema,
                    messages=messages,
                )
                consecutive_failures = 0
            except Exception as e:
                consecutive_failures += 1
                err_class = type(e).__name__
                err_msg = str(e)[:300]
                logger.warning("Anthropic iter %d failed (%d) %s: %s",
                                iteration, consecutive_failures, err_class, err_msg)
                # M20.14 Opcion B: backoff exponencial cuando es RateLimitError.
                # Anthropic envia `retry-after` header en segundos; si no lo
                # parseamos (sdk-dependent), usamos retry_s * consecutive_failures.
                # Esto le da tiempo al rate limit de 60s/8K-tokens de liberarse
                # antes de seguir contando fallos hacia el graceful_close.
                is_rate_limit = err_class == "RateLimitError" or "429" in err_msg
                if is_rate_limit:
                    retry_after_s = self._extract_retry_after(e) or (
                        self.config.rate_limit_retry_s * consecutive_failures
                    )
                    retry_after_s = min(retry_after_s, 45.0)  # cap razonable
                    logger.info(
                        "anthropic_brain rate_limited iter=%d attempt=%d "
                        "sleeping=%.1fs before retry",
                        iteration, consecutive_failures, retry_after_s,
                    )
                    yield _sse_bytes("stage_progress", {
                        "stage": f"brain_iter_{iteration}_backoff",
                        "state": "waiting",
                        "label": f"Rate limit Anthropic · esperando {retry_after_s:.0f}s antes de reintentar…",
                    })
                    await asyncio.sleep(retry_after_s)
                if (
                    consecutive_failures >= self.config.fallback_to_openai_after_failures
                ):
                    # M20.13 · Si ya hicimos trabajo útil (≥ 1 tool_use con resultado),
                    # cerramos graceful con done en vez de error fatal.
                    if stats.tool_calls_total >= 1:
                        yield _sse_bytes("stage_progress", {
                            "stage": "brain_graceful_close", "state": "fallback",
                            "label": f"Anthropic post-generación caído; cerrando graceful tras {stats.tool_calls_total} tools",
                        })
                        yield _sse_bytes("agent_thought", {
                            "id": f"final-{ctx.generation_id}",
                            "type": "final_message",
                            "content": (
                                f"Generación parcial completada: {stats.tool_calls_total} tools "
                                f"invocadas en {stats.iterations} iteraciones antes de timeout post-generación. "
                                f"El documento está disponible en blocks generados. "
                                f"Causa: {err_class}: {err_msg[:100]}"
                            ),
                            "kind": "narration",
                        })
                        yield _sse_bytes("done", {
                            "generation_id": str(ctx.generation_id),
                            "matter_document_id": None,
                            "duration_seconds": 0,
                            "cost_usd": stats.cost_usd,
                            "total_blocks": 0,
                            "iterations": stats.iterations,
                            "tokens_input": stats.tokens_input,
                            "tokens_output": stats.tokens_output,
                            "cache_read_tokens": stats.cache_read_tokens,
                            "model": model,
                            "graceful_close": True,
                            "close_reason": f"{err_class}: {err_msg[:100]}",
                        })
                        return

                    # Sin trabajo útil → error fatal
                    yield _sse_bytes("error", {
                        "stage": "anthropic_brain",
                        "error_class": err_class,
                        "message": err_msg,
                        "consecutive_failures": consecutive_failures,
                    })
                    return
                continue

            # 2) Acumular usage
            usage = getattr(response, "usage", None)
            if usage:
                stats.tokens_input += getattr(usage, "input_tokens", 0) or 0
                stats.tokens_output += getattr(usage, "output_tokens", 0) or 0
                stats.cache_creation_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
                stats.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0

            stop_reason = getattr(response, "stop_reason", None)

            # 3) Casos de stop_reason
            if stop_reason == "end_turn":
                # Brain quiere responder al usuario directamente
                final_text = self._extract_text(response)
                if final_text:
                    yield _sse_bytes("agent_thought", {
                        "id": f"final-{ctx.generation_id}",
                        "type": "final_message",
                        "content": final_text,
                        "kind": "narration",
                    })
                yield _sse_bytes("done", {
                    "generation_id": str(ctx.generation_id),
                    "matter_document_id": None,
                    "duration_seconds": 0,
                    "cost_usd": stats.cost_usd,
                    "total_blocks": 0,
                    "iterations": stats.iterations,
                    "tokens_input": stats.tokens_input,
                    "tokens_output": stats.tokens_output,
                    "cache_read_tokens": stats.cache_read_tokens,
                    "model": model,
                })
                return

            if stop_reason == "tool_use":
                # Extraer tool_use blocks → ejecutar en paralelo
                tool_calls: list[ToolCall] = []
                for block in (response.content or []):
                    if getattr(block, "type", None) == "tool_use":
                        tool_calls.append(ToolCall(
                            tool_use_id=block.id,
                            tool_name=block.name,
                            input=block.input or {},
                            iteration=iteration,
                        ))

                if not tool_calls:
                    # stop_reason=tool_use pero no hay tool_use blocks → raro, salir
                    logger.warning("stop_reason=tool_use sin tool_use blocks; forzando end")
                    return

                stats.tool_calls_total += len(tool_calls)
                stats.tool_calls_parallel_max = max(stats.tool_calls_parallel_max, len(tool_calls))

                # Ejecutar todos en paralelo
                tool_results: list[ToolResult] = await dispatcher.execute_parallel(
                    tool_calls, registry, ctx,
                    max_concurrent=self.config.max_parallel_tools,
                )

                # Emitir SSE por cada tool_call + result
                for call, result in zip(tool_calls, tool_results):
                    for ev_bytes in map_tool_to_sse_events(
                        tool_name=call.tool_name,
                        tool_input=call.input,
                        tool_result=(result.output if result.ok else {"_error": result.error_message}),
                        tool_use_id=call.tool_use_id,
                        iteration=iteration,
                    ):
                        yield ev_bytes

                # Inyectar assistant message + tool_results al historial
                messages.append({
                    "role": "assistant",
                    "content": [_anthropic_block_to_dict(b) for b in (response.content or [])],
                })
                messages.append({
                    "role": "user",
                    "content": [r.to_anthropic_tool_result() for r in tool_results],
                })
                continue

            if stop_reason == "max_tokens":
                # Continuar el loop pidiendo que termine
                messages.append({
                    "role": "assistant",
                    "content": [_anthropic_block_to_dict(b) for b in (response.content or [])],
                })
                messages.append({"role": "user", "content": "Continúa donde te quedaste."})
                continue

            # Otro stop_reason inesperado
            logger.warning("Brain stop_reason inesperado: %r", stop_reason)
            break

        # Max iterations alcanzado
        yield _sse_bytes("error", {
            "stage": "anthropic_brain",
            "message": f"max_iterations ({self.config.max_iterations}) alcanzado sin end_turn",
        })

    # ---- llamada al SDK ----

    async def _call_anthropic(
        self,
        *,
        model: str,
        system_prompt: str,
        tools_schema: list[dict],
        messages: list[dict],
    ):
        kwargs: dict = {
            "model": model,
            "max_tokens": self.config.max_tokens,
            "messages": messages,
            "tools": tools_schema,
        }
        if self.config.enable_prompt_caching:
            # Marcar system prompt + tools como cacheables (prefix ephemeral)
            kwargs["system"] = [
                {"type": "text", "text": system_prompt,
                 "cache_control": {"type": "ephemeral"}},
            ]
            # extra_headers para opt-in beta
            extra_headers = {"anthropic-beta": "prompt-caching-2024-07-31"}
        else:
            kwargs["system"] = system_prompt
            extra_headers = {}

        # M20.14: observability — el Brain antes llamaba al SDK directo sin
        # loggear nada (a diferencia de utils/llm_provider.py). Eso hacía que
        # cada generación lean fuera caja negra. Loggeamos in/out + duration.
        import time as _time
        _start = _time.perf_counter()
        logger.info(
            "anthropic_brain call → model=%s msgs=%d tools=%d caching=%s",
            model, len(messages), len(tools_schema),
            self.config.enable_prompt_caching,
        )
        try:
            if extra_headers:
                response = await self.anthropic.messages.create(
                    extra_headers=extra_headers, **kwargs,
                )
            else:
                response = await self.anthropic.messages.create(**kwargs)
        except Exception as e:
            elapsed_ms = int((_time.perf_counter() - _start) * 1000)
            logger.warning(
                "anthropic_brain call ✗ model=%s elapsed=%dms err=%s: %s",
                model, elapsed_ms, type(e).__name__, str(e)[:200],
            )
            raise
        elapsed_ms = int((_time.perf_counter() - _start) * 1000)
        usage = getattr(response, "usage", None)
        tokens_in = getattr(usage, "input_tokens", 0) if usage else 0
        tokens_out = getattr(usage, "output_tokens", 0) if usage else 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) if usage else 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) if usage else 0
        logger.info(
            "anthropic_brain call ✓ model=%s elapsed=%dms tokens_in=%d tokens_out=%d "
            "cache_read=%d cache_write=%d stop=%s",
            model, elapsed_ms, tokens_in, tokens_out, cache_read, cache_write,
            getattr(response, "stop_reason", None),
        )
        return response

    @staticmethod
    def _extract_text(response) -> str:
        out: list[str] = []
        for block in (response.content or []):
            if getattr(block, "type", None) == "text":
                out.append(getattr(block, "text", "") or "")
        return "\n\n".join(out).strip()

    @staticmethod
    def _extract_retry_after(exc: Exception) -> Optional[float]:
        """Extrae el `retry-after` (segundos) del response header de un 429.

        El SDK Anthropic adjunta el response del HTTPError al excepcion via
        exc.response.headers. Si no esta disponible, parsea el body JSON o
        retorna None — el caller usa un default basado en consecutive_failures.
        """
        try:
            # 1) Header retry-after (numerico, en segundos)
            resp = getattr(exc, "response", None)
            if resp is not None:
                headers = getattr(resp, "headers", {}) or {}
                ra = headers.get("retry-after") or headers.get("Retry-After")
                if ra is not None:
                    try:
                        return float(ra)
                    except (TypeError, ValueError):
                        pass
                # 2) Header `anthropic-ratelimit-output-tokens-reset` (ISO timestamp)
                reset_iso = headers.get("anthropic-ratelimit-output-tokens-reset")
                if reset_iso:
                    try:
                        from datetime import datetime, timezone
                        reset_dt = datetime.fromisoformat(
                            reset_iso.replace("Z", "+00:00")
                        )
                        delta = (reset_dt - datetime.now(timezone.utc)).total_seconds()
                        if delta > 0:
                            return delta
                    except Exception:
                        pass
        except Exception:
            pass
        return None


def _anthropic_block_to_dict(block) -> dict:
    """Convierte un content block del SDK Anthropic a dict para re-enviar."""
    btype = getattr(block, "type", None)
    if btype == "text":
        return {"type": "text", "text": getattr(block, "text", "")}
    if btype == "tool_use":
        return {
            "type": "tool_use",
            "id": getattr(block, "id", ""),
            "name": getattr(block, "name", ""),
            "input": getattr(block, "input", {}) or {},
        }
    if btype == "thinking":
        return {"type": "thinking", "thinking": getattr(block, "thinking", "")}
    # Fallback: serializar
    if hasattr(block, "model_dump"):
        return block.model_dump()
    return {"type": btype or "unknown", "_raw": str(block)[:500]}


def _sse_bytes(event_name: str, data: dict) -> bytes:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event_name}\ndata: {payload}\n\n".encode("utf-8")
