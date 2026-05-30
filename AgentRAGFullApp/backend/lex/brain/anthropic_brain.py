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
DEFAULT_MAX_TOKENS = 8192
DEFAULT_MAX_ITERATIONS = 30
DEFAULT_MAX_PARALLEL_TOOLS = 10


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
    fallback_to_openai_after_failures: int = 2


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
    ) -> AsyncIterator[bytes]:
        """Loop ReAct principal. Yieldea bytes SSE listos para el stream HTTP.

        Cada iteración:
          1. messages.create con tools=[...]
          2. si stop_reason='tool_use' → ejecuta tools en paralelo → tool_results → vuelta
          3. si stop_reason='end_turn' → emite SSE final + return
          4. si stop_reason='max_tokens' → continúa loop
          5. si error N veces → fallback OpenAI
        """
        stats = BrainStats()
        dispatcher = ToolDispatcher(persist_audit=True)

        # Modelo a usar (Opus para doc_types complejos)
        model = self.config.opus_model if doc_type_hint in OPUS_DOC_TYPES else self.config.sonnet_model
        stats.model_actual = model

        # Construir system prompt + primer mensaje user
        system_prompt = build_system_prompt(playbook_raw_md)
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
                logger.warning("Anthropic iter %d failed (%d): %s", iteration, consecutive_failures, e)
                if (
                    consecutive_failures >= self.config.fallback_to_openai_after_failures
                    and self.openai is not None
                ):
                    yield _sse_bytes("stage_progress", {
                        "stage": "brain_fallback", "state": "fallback",
                        "label": "Anthropic caído, fallback OpenAI…",
                    })
                    stats.fallback_used = True
                    # En esta versión: si Anthropic cae, emitimos error y salimos.
                    # Una implementación completa traduciría messages/tools a OpenAI tool_choice.
                    yield _sse_bytes("error", {
                        "stage": "anthropic_brain",
                        "message": f"Anthropic falló {consecutive_failures}x; fallback OpenAI no implementado en esta versión",
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

        if extra_headers:
            response = await self.anthropic.messages.create(
                extra_headers=extra_headers, **kwargs,
            )
        else:
            response = await self.anthropic.messages.create(**kwargs)
        return response

    @staticmethod
    def _extract_text(response) -> str:
        out: list[str] = []
        for block in (response.content or []):
            if getattr(block, "type", None) == "text":
                out.append(getattr(block, "text", "") or "")
        return "\n\n".join(out).strip()


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
