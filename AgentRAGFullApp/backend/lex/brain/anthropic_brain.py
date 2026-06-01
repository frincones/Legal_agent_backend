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
# M20.14 Opcion 1 + B: cuenta Anthropic en Build Tier 1.
# Rate limits separados por modelo (cada uno tiene su pool):
#   Sonnet 4.x:  30K ITPM /  8K OTPM
#   Haiku 4.5:   50K ITPM / 10K OTPM (67% mas ITPM, 25% mas OTPM)
#   Opus 4.x:   500K ITPM / 80K OTPM (16x ITPM, 10x OTPM)
# Estrategia:
#   - Brain ReAct loop (planning, tool selection) → Haiku 4.5 (rapido, suficiente
#     para decisiones discretas; suma su pool al de Sonnet que usan los tools
#     internos como generate_clause).
#   - generate_clause y verify_citation (calidad legal) → Sonnet/OpenAI (sin cambio).
# Resultado: capacidad combinada efectiva ~80K ITPM Tier 1.
DEFAULT_MAX_TOKENS = 4096
DEFAULT_MAX_ITERATIONS = 30
DEFAULT_MAX_PARALLEL_TOOLS = 3
DEFAULT_RATE_LIMIT_RETRY_S = 8.0


async def _persist_blocks_for_lean(
    pool,
    *,
    generation_id: Any,
    blocks: list[dict],
) -> Optional[str]:
    """M21.HOTFIX-7: persiste los blocks acumulados del path Lean en document_blocks.

    Genera un document_id nuevo (uuid4) y llama BlocksRepo.insert_blocks_batch.
    Retorna el document_id si se persistió OK, None si pool no disponible o no hay blocks.

    Sin esto, el chat-over-document NO funciona porque el endpoint /chat consulta
    document_blocks por document_id, que devuelve 0 rows.
    """
    if pool is None or not blocks:
        logger.info(
            "_persist_blocks_for_lean: skipping (pool=%s blocks=%d)",
            pool is not None, len(blocks),
        )
        return None
    try:
        from lex.storage import BlocksRepo
        new_doc_id = str(uuid4())
        repo = BlocksRepo(pool)
        inserted = await repo.insert_blocks_batch(
            document_id=new_doc_id,
            generation_id=str(generation_id),
            blocks=blocks,
        )
        logger.info(
            "_persist_blocks_for_lean: persisted %d/%d blocks for generation=%s → document=%s",
            inserted, len(blocks), generation_id, new_doc_id,
        )
        return new_doc_id if inserted > 0 else None
    except Exception as e:
        logger.exception("_persist_blocks_for_lean failed: %s", e)
        return None


async def _resolve_document_id_from_blocks(pool, generation_id: Any) -> Optional[str]:
    """M21.HOTFIX-3: resuelve el document_id real desde document_blocks por generation_id.

    Antes el evento SSE 'done' emitía matter_document_id=None hardcoded, lo que
    causaba que state.documentId en el frontend quedara null y el chat composer
    hiciera early-return con "Espera a que termine la generación".

    Ahora consultamos la tabla document_blocks (que insert_blocks_batch llena
    con (document_id, generation_id) durante la generación) para recuperar
    el document_id real.

    HOTFIX-6: log INFO siempre para diagnostico. Si query devuelve None,
    intenta fallback por matter_documents (algunos paths persisten ahi).
    """
    if pool is None or generation_id is None:
        logger.warning(
            "_resolve_document_id_from_blocks: pool=%s generation_id=%s — early return None",
            pool is not None, generation_id,
        )
        return None
    gen_id_str = str(generation_id)
    try:
        async with pool.acquire() as conn:
            # Primary: document_blocks
            row = await conn.fetchrow(
                "SELECT document_id FROM document_blocks "
                "WHERE generation_id = $1::uuid "
                "ORDER BY block_order ASC LIMIT 1",
                gen_id_str,
            )
            if row and row["document_id"]:
                doc_id = str(row["document_id"])
                logger.info(
                    "_resolve_document_id_from_blocks: generation=%s → doc_id=%s (via document_blocks)",
                    gen_id_str, doc_id,
                )
                return doc_id

            # Fallback 1: contar cuantos blocks hay para ese generation_id
            count_row = await conn.fetchrow(
                "SELECT count(*) as n FROM document_blocks WHERE generation_id = $1::uuid",
                gen_id_str,
            )
            n_blocks = count_row["n"] if count_row else 0
            logger.warning(
                "_resolve_document_id_from_blocks: generation=%s NO doc_id en document_blocks. "
                "count=%d. Intentando fallback matter_documents...",
                gen_id_str, n_blocks,
            )

            # Fallback 2: matter_documents (algunos paths persisten ahi en vez de document_blocks)
            try:
                row2 = await conn.fetchrow(
                    "SELECT id FROM matter_documents "
                    "WHERE generation_id = $1::uuid "
                    "ORDER BY created_at ASC LIMIT 1",
                    gen_id_str,
                )
                if row2 and row2["id"]:
                    doc_id = str(row2["id"])
                    logger.info(
                        "_resolve_document_id_from_blocks: generation=%s → doc_id=%s (via matter_documents fallback)",
                        gen_id_str, doc_id,
                    )
                    return doc_id
            except Exception as e2:
                logger.debug("matter_documents fallback query failed: %s", e2)

            logger.warning(
                "_resolve_document_id_from_blocks: generation=%s NO doc_id encontrado en ninguna tabla",
                gen_id_str,
            )
            return None
    except Exception as e:
        logger.warning("_resolve_document_id_from_blocks(%s) exception: %s", gen_id_str, e)
        return None


@dataclass
class BrainConfig:
    # M20.14 Opcion 1: routing por modelo según complejidad.
    # `routing_model` es el modelo DEFAULT del Brain ReAct loop (planning).
    # `sonnet_model` queda como modelo de fallback para doc_types complejos.
    routing_model: str = "claude-haiku-4-5"   # Brain planning (50K ITPM Tier 1)
    sonnet_model: str = "claude-sonnet-4-6"   # legacy / fallback medio
    opus_model: str = "claude-opus-4-7"       # doc_types complejos (500K ITPM)
    fallback_openai_model: str = "gpt-4o"
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    max_parallel_tools: int = DEFAULT_MAX_PARALLEL_TOOLS
    enable_prompt_caching: bool = True
    enable_parallel_tools: bool = True
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

        # M21.HOTFIX-7: ACUMULADOR DE BLOCKS para persistencia en BD.
        # El path Lean nunca llamaba insert_blocks_batch, por lo que después
        # de generar el documento NO se podía chatear sobre él (chat endpoint
        # consulta document_blocks por document_id, que devolvía 0 rows).
        # Aquí accumulamos cada block emitido por generate_clause y al final
        # (antes del done event) los persistimos con un document_id nuevo.
        accumulated_blocks: list[dict] = []
        accumulated_document_id: Optional[str] = None
        block_order_counter: int = 0

        # M20.14 Opcion 1: routing por complejidad.
        # - Doc types complejos (casacion, demanda_compleja, etc.) → Opus 4.x
        #   (capacidad 500K ITPM Tier 1, calidad maxima, mas caro).
        # - Resto (default) → Haiku 4.5 (planning rapido, 50K ITPM Tier 1,
        #   barato, suficiente para decidir tool_use). Los generate_clause
        #   internos siguen usando Sonnet/OpenAI para calidad del contenido legal.
        if doc_type_hint in OPUS_DOC_TYPES:
            model = self.config.opus_model
        else:
            model = self.config.routing_model
        stats.model_actual = model
        logger.info("react_loop: usando model=%s (doc_type_hint=%r)", model, doc_type_hint)

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
                        # M21.HOTFIX-7: persistir accumulated_blocks ANTES del done
                        # event para que el chat-over-document pueda recuperarlos.
                        persisted_doc_id = await _persist_blocks_for_lean(
                            ctx.pool,
                            generation_id=ctx.generation_id,
                            blocks=accumulated_blocks,
                        )
                        # Fallback: si por alguna razon no se persistio, intentar resolver
                        # desde tablas existentes (legacy path quizas)
                        resolved_doc_id = persisted_doc_id or await _resolve_document_id_from_blocks(
                            ctx.pool, ctx.generation_id,
                        )
                        yield _sse_bytes("done", {
                            "generation_id": str(ctx.generation_id),
                            "matter_document_id": resolved_doc_id,
                            "duration_seconds": 0,
                            "cost_usd": stats.cost_usd,
                            "total_blocks": len(accumulated_blocks),
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
                # M21.HOTFIX-7: persistir accumulated_blocks ANTES del done event
                # para que el chat-over-document funcione (endpoint /chat consulta
                # document_blocks por document_id).
                persisted_doc_id = await _persist_blocks_for_lean(
                    ctx.pool,
                    generation_id=ctx.generation_id,
                    blocks=accumulated_blocks,
                )
                # Fallback: si no se persistio, resolver desde tablas legacy
                resolved_doc_id = persisted_doc_id or await _resolve_document_id_from_blocks(
                    ctx.pool, ctx.generation_id,
                )
                yield _sse_bytes("done", {
                    "generation_id": str(ctx.generation_id),
                    "matter_document_id": resolved_doc_id,
                    "duration_seconds": 0,
                    "cost_usd": stats.cost_usd,
                    "total_blocks": len(accumulated_blocks),
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
                    # M21.HOTFIX-7: acumular blocks de generate_clause para persistencia
                    if call.tool_name == "generate_clause" and result.ok:
                        out = result.output or {}
                        section_key = call.input.get("section_key", "default")
                        section_blocks = out.get("blocks") or []
                        for b in section_blocks:
                            if not isinstance(b, dict):
                                continue
                            bid = b.get("block_id")
                            btype = b.get("type") or b.get("block_type")
                            if not bid or not btype:
                                continue
                            # block_data = todo el block sin block_id (que ya es column)
                            block_data_payload = {k: v for k, v in b.items() if k != "block_id"}
                            accumulated_blocks.append({
                                "block_id": bid,
                                "section_key": section_key,
                                "block_order": block_order_counter,
                                "block_type": btype,
                                "block_data": block_data_payload,
                            })
                            block_order_counter += 1

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
        # M20.14 Camino 3B: validar y reparar messages ANTES de llamar al API.
        # Anthropic rechaza con BadRequestError 400 si cualquier tool_use block
        # de un assistant message no tiene su tool_result correspondiente en
        # el siguiente user message. Si por cualquier motivo el dispatcher
        # perdio un tool_result (excepcion no controlada, race condition,
        # truncamiento), inyectamos un tool_result sintetico con is_error=true
        # para preservar el invariante. Esto evita el "messages.N: tool_use
        # ids were found without tool_result blocks immediately after".
        try:
            repaired_count = self._repair_tool_pairing(messages)
            if repaired_count:
                logger.warning(
                    "anthropic_brain: reparados %d tool_use sin tool_result "
                    "(inyectados como is_error=true)", repaired_count,
                )
        except Exception as e:
            # Nunca dejar que el validador rompa el flujo principal.
            logger.exception("anthropic_brain _repair_tool_pairing fallo (continua sin reparar): %s", e)

        kwargs: dict = {
            "model": model,
            "max_tokens": self.config.max_tokens,
            "messages": messages,
            "tools": tools_schema,
        }
        if self.config.enable_prompt_caching:
            # M20.14 Opcion 3: caching maximizado en 3 niveles.
            # - system: TTL 1h (mas barato amortizado, este texto NO cambia
            #   entre generaciones de la misma firm dentro de 1h).
            # - tools (ultimo tool): cache_control en el ULTIMO tool del
            #   schema cachea TODO el array (18 tool definitions ~3K tokens).
            # - messages: cache_control en el ULTIMO bloque del ULTIMO
            #   mensaje con TTL 5min — captura la conversacion para reuso
            #   en iteraciones consecutivas del ReAct loop.
            # Resultado: ~70-80% cache hit rate esperado, baja ITPM 5x.
            #
            # cache_read_input_tokens NO cuenta para ITPM rate limit —
            # esto efectivamente multiplica nuestra capacidad efectiva.
            kwargs["system"] = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                },
            ]
            # Cachear el array de tools: cache_control en el ULTIMO tool
            # marca el breakpoint y cachea TODO el array hasta ahi.
            if tools_schema:
                cached_tools = [dict(t) for t in tools_schema]
                cached_tools[-1] = {
                    **cached_tools[-1],
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                }
                kwargs["tools"] = cached_tools
            # Cachear el ULTIMO bloque del ULTIMO mensaje del historial
            # (5min TTL — captura la conversacion en curso). Esto solo aplica
            # si el ultimo content es lista (multi-block); strings no soportan
            # cache_control.
            try:
                if messages:
                    last_msg = messages[-1]
                    last_content = last_msg.get("content")
                    if isinstance(last_content, list) and last_content:
                        last_msg_copy = dict(last_msg)
                        new_content = [dict(b) if isinstance(b, dict) else b
                                         for b in last_content]
                        if isinstance(new_content[-1], dict):
                            new_content[-1] = {
                                **new_content[-1],
                                "cache_control": {"type": "ephemeral"},
                            }
                            last_msg_copy["content"] = new_content
                            kwargs["messages"] = messages[:-1] + [last_msg_copy]
            except Exception as e:
                # Si falla el caching de messages, no rompe — sigue sin cache_control.
                logger.debug("messages cache_control skip: %s", e)
            # extra_headers: beta para 1h TTL (extended-cache-ttl-2025-04-11)
            extra_headers = {
                "anthropic-beta": "prompt-caching-2024-07-31,extended-cache-ttl-2025-04-11",
            }
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
        # M20.14 Opcion 3: cache hit rate observability.
        # hit_rate = cache_read / (cache_read + input_tokens) según best practice
        # del equipo de Claude Code (>60% = bueno, >80% = optimo).
        total_in_no_write = cache_read + tokens_in
        hit_rate_pct = round(100.0 * cache_read / total_in_no_write, 1) if total_in_no_write > 0 else 0.0
        # ITPM rate-counted (cache_read NO cuenta hacia ITPM).
        itpm_counted = tokens_in + cache_write
        logger.info(
            "anthropic_brain call ✓ model=%s elapsed=%dms tokens_in=%d tokens_out=%d "
            "cache_read=%d cache_write=%d hit_rate=%.1f%% itpm_counted=%d stop=%s",
            model, elapsed_ms, tokens_in, tokens_out, cache_read, cache_write,
            hit_rate_pct, itpm_counted,
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
    def _repair_tool_pairing(messages: list[dict]) -> int:
        """Repara el invariante tool_use ↔ tool_result que Anthropic exige.

        Walks `messages` y para cada `tool_use` block en un assistant message,
        verifica que el SIGUIENTE user message tenga un `tool_result` block
        con el mismo `tool_use_id`. Si falta, lo inyecta como is_error=true.

        También limpia tool_result blocks "huérfanos" (que no matchean ningún
        tool_use del assistant anterior), que también disparan 400.

        Returns:
            int: número de tool_results inyectados/sanitizados.
        """
        if not messages:
            return 0
        repaired = 0
        i = 0
        while i < len(messages) - 1:
            msg = messages[i]
            next_msg = messages[i + 1]
            # Solo procesamos pares (assistant con tool_use → user con tool_result)
            if msg.get("role") != "assistant":
                i += 1
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                i += 1
                continue
            tool_use_ids: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tid = block.get("id")
                    if tid:
                        tool_use_ids.append(tid)
            if not tool_use_ids:
                i += 1
                continue
            # next_msg debe ser user con tool_result blocks
            if next_msg.get("role") != "user":
                # Inyectar un user message completo si falta — pero esto es
                # señal de bug serio. Lo logueamos y reparamos.
                injected = [
                    {
                        "type": "tool_result",
                        "tool_use_id": tid,
                        "content": "tool_result missing (synthetic placeholder · dispatcher lost result)",
                        "is_error": True,
                    }
                    for tid in tool_use_ids
                ]
                messages.insert(i + 1, {"role": "user", "content": injected})
                repaired += len(injected)
                i += 2
                continue
            next_content = next_msg.get("content")
            if not isinstance(next_content, list):
                # user message con content=str pero el assistant tenia tool_use:
                # reemplazar por una lista con todos los tool_results sinteticos.
                injected = [
                    {
                        "type": "tool_result",
                        "tool_use_id": tid,
                        "content": "tool_result missing (synthetic placeholder · user content was not list)",
                        "is_error": True,
                    }
                    for tid in tool_use_ids
                ]
                next_msg["content"] = injected
                repaired += len(injected)
                i += 2
                continue
            present_ids = {
                b.get("tool_use_id")
                for b in next_content
                if isinstance(b, dict) and b.get("type") == "tool_result"
            }
            missing = [tid for tid in tool_use_ids if tid not in present_ids]
            for tid in missing:
                next_content.insert(0, {
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": (
                        f"tool_result missing for {tid} (synthetic placeholder · "
                        "dispatcher did not produce a result for this tool_use)"
                    ),
                    "is_error": True,
                })
                repaired += 1
            # Limpiar tool_result blocks huérfanos (sin matching tool_use)
            valid_ids = set(tool_use_ids)
            cleaned: list = []
            removed_orphans = 0
            for b in next_content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    if b.get("tool_use_id") not in valid_ids:
                        removed_orphans += 1
                        continue
                cleaned.append(b)
            if removed_orphans:
                next_msg["content"] = cleaned
                repaired += removed_orphans
            i += 2
        return repaired

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
