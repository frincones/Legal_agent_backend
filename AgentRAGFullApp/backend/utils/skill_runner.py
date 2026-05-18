"""Sprint E · Skill runner · forked agent ejecutor de skills legales.

Patrón:
  1. Resolver skill (custom firma > builtin)
  2. Cargar playbook firma + inyectar en system prompt
  3. Pre-skill hooks (linter, validators)
  4. Llamar OpenAI con structured output schema
  5. Post-skill hooks (citation verifier, severity classifier)
  6. Persist en skill_executions (audit)
  7. Return structured result

Budget aislado por skill (no consume context principal del voice agent).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional
from uuid import UUID, uuid4

from utils.skill_loader import SkillDefinition, resolve_skill
from utils.playbook_resolver import get_firm_playbook, playbook_context_block
from utils.hook_runner import run_hooks_for_skill

logger = logging.getLogger(__name__)

MAX_TOKENS_BUDGET = 16000
DEFAULT_TIMEOUT = 60.0
# Max iterations of the tool-calling loop · prevents runaway in case the model
# keeps requesting tools. Same value used by agent.subagents.base.BaseSubAgent.
MAX_TOOL_ITERATIONS = 6


def _resolve_chat_tools(skill: SkillDefinition) -> list[dict[str, Any]]:
    """Build OpenAI Chat tools list from skill.frontmatter['allowed-tools'].

    Reads the global voice _tool_registry / _tool_descriptors (same source the
    voice agent + subagents use) so a chat skill and a voice tool stay in sync.

    Returns [] when the skill doesn't declare any tools (= single-shot mode,
    backward compatible with every existing skill).
    """
    allowed = skill.allowed_tools
    if not allowed:
        return []
    try:
        from api.voice import _tool_descriptors
        descriptors = _tool_descriptors()
    except Exception as e:
        logger.warning("skill_runner could not load _tool_descriptors: %s", e)
        return []

    # Filter by name. Support "*" wildcard for skills that want everything
    # (e.g. internal admin skills · not recommended for user-invocable ones).
    if "*" in allowed:
        return [{"type": "function", "function": d} for d in descriptors]
    name_set = set(allowed)
    return [
        {"type": "function", "function": d}
        for d in descriptors
        if d.get("name") in name_set
    ]


async def _execute_tool_call(
    fn_name: str,
    fn_args: dict[str, Any],
    skill: SkillDefinition,
    ctx: dict[str, Any],
) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
    """Run one tool from the global _tool_registry.

    Returns (llm_result, ui_command):
      - llm_result: dict sent back to the model (with _ui_command stripped so
        the LLM doesn't see implementation noise).
      - ui_command: the {action, ...} payload to forward to the frontend via
        the SSE stream, or None if the tool didn't emit one. Caller is
        responsible for emitting a `ui_command` SSE event.
    """
    try:
        from api.voice import _tool_registry
    except Exception as e:
        return {"error": f"tool_registry unavailable: {e}"}, None

    allowed = set(skill.allowed_tools)
    is_wildcard = "*" in allowed
    if not is_wildcard and fn_name not in allowed:
        return {"error": f"tool '{fn_name}' not allowed for skill {skill.command}"}, None

    fn = _tool_registry.get(fn_name)
    if fn is None:
        return {"error": f"tool '{fn_name}' not registered"}, None

    try:
        result = await fn(args=fn_args, ctx=ctx)
    except Exception as e:
        logger.exception("skill_runner tool %s raised: %s", fn_name, e)
        return {"error": str(e)[:240]}, None

    ui_command: Optional[dict[str, Any]] = None
    if isinstance(result, dict) and "_ui_command" in result:
        ui_command = result.get("_ui_command")
        result = {k: v for k, v in result.items() if k != "_ui_command"}
    return result, ui_command


async def run_skill_stream(
    pool,
    *,
    firm_id: str,
    user_id: str,
    command: str,
    input_data: dict[str, Any],
    matter_id: Optional[str] = None,
    document_id: Optional[str] = None,
) -> AsyncIterator[dict[str, Any]]:
    """Versión streaming de run_skill · yield eventos para SSE.

    Eventos emitidos:
      {"event": "meta", "data": {execution_id, command, skill_id, is_custom}}
      {"event": "blocked", "data": {hook, reason}}             # si pre-hook bloquea
      {"event": "delta", "data": {"text": "...chunk..."}}      # tokens streaming
      {"event": "warning", "data": {hook, level, reason}}      # post-hooks
      {"event": "done", "data": {duration_ms, tokens, full_text, warnings}}
      {"event": "error", "data": {error, detail}}              # si falla
    """
    started = time.time()
    execution_id = str(uuid4())

    skill = await resolve_skill(pool, firm_id, command)
    if not skill:
        yield {"event": "error", "data": {"error": "skill_not_found", "command": command}}
        return

    yield {
        "event": "meta",
        "data": {
            "execution_id": execution_id,
            "command": command,
            "skill_id": str(skill.id) if skill.id else None,
            "is_custom": skill.is_custom,
            "name": skill.name,
        },
    }

    playbook = await get_firm_playbook(pool, firm_id)
    playbook_block = playbook_context_block(playbook)

    pre_decisions, pre_hooks_fired = await run_hooks_for_skill(
        pool, command, "pre_skill",
        context={
            "input": input_data, "playbook": playbook,
            "firm_id": firm_id, "user_id": user_id, "matter_id": matter_id,
        },
    )
    for d in pre_decisions:
        if d.get("decision") == "block":
            await _persist_execution(
                pool, execution_id, firm_id, user_id, skill.id, command,
                matter_id, document_id, input_data, {},
                "blocked_by_hook", d.get("reason", "blocked"), pre_hooks_fired,
                0, 0, 0, 0,
            )
            yield {
                "event": "blocked",
                "data": {"hook": d.get("hook_key"), "reason": d.get("reason")},
            }
            return

    full_system_prompt = (
        skill.system_prompt + "\n\n" + playbook_block + "\n\n" + (skill.references_md or "")
    )
    user_message = _format_user_message(skill, input_data)

    full_text_parts: list[str] = []
    tokens_in = 0
    tokens_out = 0

    # Resolve tool calling from skill.frontmatter["allowed-tools"].
    # When the skill declares zero tools, we use the original single-shot
    # streaming path (backward compatible).
    chat_tools = _resolve_chat_tools(skill)

    try:
        from openai import AsyncOpenAI
        import os
        client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        if not chat_tools:
            # ── Original path · single-shot streaming, no tools ──
            kwargs: dict[str, Any] = {
                "model": skill.model,
                "messages": [
                    {"role": "system", "content": full_system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "temperature": 0.3,
                "max_tokens": MAX_TOKENS_BUDGET,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            stream_resp = await client.chat.completions.create(**kwargs)
            async for chunk in stream_resp:
                if chunk.usage:
                    tokens_in = chunk.usage.prompt_tokens or 0
                    tokens_out = chunk.usage.completion_tokens or 0
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content
                if delta:
                    full_text_parts.append(delta)
                    yield {"event": "delta", "data": {"text": delta}}
        else:
            # ── Tool-calling streaming path · multi-round ──
            # Pattern from agent.subagents.base.BaseSubAgent.run, adapted for
            # streaming. Each round:
            #   1. Stream the assistant message (collecting tool_call deltas)
            #   2. If tool_calls exist, execute them and append tool messages
            #   3. Loop · stop when no more tool_calls or MAX_TOOL_ITERATIONS
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": full_system_prompt},
                {"role": "user", "content": user_message},
            ]
            sub_ctx = {
                "firm_id": firm_id,
                "user_id": user_id,
                "matter_id": matter_id,
                "document_id": document_id,
                "subagent_chain": ["chat_skill"],
                # canvas tools (find_replace, replace_section, ...) need the
                # canvas text in ctx so they can validate the needle BEFORE
                # claiming success to the LLM.
                "document_text": input_data.get("document_text"),
            }
            done_calling = False
            for round_idx in range(MAX_TOOL_ITERATIONS):
                if done_calling:
                    break
                kwargs = {
                    "model": skill.model,
                    "messages": messages,
                    "tools": chat_tools,
                    "tool_choice": "auto",
                    "temperature": 0.3,
                    "max_tokens": MAX_TOKENS_BUDGET,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                }
                stream_resp = await client.chat.completions.create(**kwargs)

                # Accumulate streamed content + tool_call deltas for this round.
                round_content_parts: list[str] = []
                tool_calls_acc: dict[int, dict[str, Any]] = {}
                async for chunk in stream_resp:
                    if chunk.usage:
                        tokens_in += chunk.usage.prompt_tokens or 0
                        tokens_out += chunk.usage.completion_tokens or 0
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta.content:
                        round_content_parts.append(delta.content)
                        full_text_parts.append(delta.content)
                        yield {"event": "delta", "data": {"text": delta.content}}
                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            idx = tc.index
                            slot = tool_calls_acc.setdefault(idx, {
                                "id": tc.id,
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            })
                            if tc.id:
                                slot["id"] = tc.id
                            if tc.function:
                                if tc.function.name:
                                    slot["function"]["name"] = tc.function.name
                                if tc.function.arguments:
                                    slot["function"]["arguments"] += tc.function.arguments

                # Build the assistant message (with any tool_calls).
                assembled_calls = [tool_calls_acc[k] for k in sorted(tool_calls_acc)]
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": "".join(round_content_parts) or None,
                }
                if assembled_calls:
                    assistant_msg["tool_calls"] = assembled_calls
                messages.append(assistant_msg)

                if not assembled_calls:
                    done_calling = True
                    break

                # Execute each tool call · feed results back into messages.
                for tc in assembled_calls:
                    fn_name = tc["function"]["name"]
                    raw_args = tc["function"]["arguments"] or "{}"
                    try:
                        fn_args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        fn_args = {}

                    yield {
                        "event": "tool_started",
                        "data": {"name": fn_name, "round": round_idx + 1},
                    }
                    tool_result, ui_command = await _execute_tool_call(
                        fn_name, fn_args, skill, sub_ctx
                    )
                    yield {
                        "event": "tool_finished",
                        "data": {
                            "name": fn_name,
                            "round": round_idx + 1,
                            "ok": "error" not in tool_result,
                            "preview": json.dumps(tool_result, ensure_ascii=False, default=str)[:200],
                        },
                    }
                    if ui_command:
                        yield {"event": "ui_command", "data": ui_command}
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps(tool_result, ensure_ascii=False, default=str),
                    })
            # End of tool-calling loop.
    except Exception as e:
        logger.warning("skill_runner stream OpenAI failed: %s", e)
        await _persist_execution(
            pool, execution_id, firm_id, user_id, skill.id, command,
            matter_id, document_id, input_data, {},
            "error", str(e)[:240], pre_hooks_fired,
            int((time.time() - started) * 1000), 0, 0, 0,
        )
        yield {"event": "error", "data": {"error": "llm_failed", "detail": str(e)[:240]}}
        return

    content = "".join(full_text_parts)
    parsed_output = {"text": content}

    post_decisions, post_hooks_fired = await run_hooks_for_skill(
        pool, command, "post_skill",
        context={
            "input": input_data, "output": parsed_output, "playbook": playbook,
            "firm_id": firm_id, "user_id": user_id, "matter_id": matter_id,
        },
    )
    warnings: list[dict[str, Any]] = []
    for d in post_decisions:
        if d.get("decision") in ("warn", "block"):
            warning = {
                "hook": d.get("hook_key"),
                "level": d.get("decision"),
                "reason": d.get("reason"),
            }
            warnings.append(warning)
            yield {"event": "warning", "data": warning}

    duration_ms = int((time.time() - started) * 1000)
    await _persist_execution(
        pool, execution_id, firm_id, user_id, skill.id, command,
        matter_id, document_id,
        {"keys": list(input_data.keys())},
        {"keys": list(parsed_output.keys()), "warning_count": len(warnings)},
        "success", None, pre_hooks_fired + post_hooks_fired,
        duration_ms, tokens_in, tokens_out,
        _estimate_cost_cents(tokens_in, tokens_out, skill.model),
    )

    yield {
        "event": "done",
        "data": {
            "execution_id": execution_id,
            "duration_ms": duration_ms,
            "tokens": {"input": tokens_in, "output": tokens_out},
            "full_text": content,
            "warnings": warnings,
        },
    }


async def run_skill(
    pool,
    *,
    firm_id: str,
    user_id: str,
    command: str,
    input_data: dict[str, Any],
    matter_id: Optional[str] = None,
    document_id: Optional[str] = None,
    stream: bool = False,
) -> dict[str, Any]:
    """Ejecuta skill end-to-end · retorna dict con result o error."""
    started = time.time()
    execution_id = str(uuid4())

    skill = await resolve_skill(pool, firm_id, command)
    if not skill:
        await _persist_execution(
            pool, execution_id, firm_id, user_id, None, command,
            matter_id, document_id, {}, {},
            "error", f"skill_not_found: {command}", [], 0, 0, 0, 0,
        )
        return {"ok": False, "error": "skill_not_found", "command": command}

    # Playbook context
    playbook = await get_firm_playbook(pool, firm_id)
    playbook_block = playbook_context_block(playbook)

    # Pre-skill hooks
    pre_decisions, pre_hooks_fired = await run_hooks_for_skill(
        pool, command, "pre_skill",
        context={
            "input": input_data, "playbook": playbook,
            "firm_id": firm_id, "user_id": user_id, "matter_id": matter_id,
        },
    )
    for d in pre_decisions:
        if d.get("decision") == "block":
            await _persist_execution(
                pool, execution_id, firm_id, user_id, skill.id, command,
                matter_id, document_id, input_data, {},
                "blocked_by_hook", d.get("reason", "blocked"), pre_hooks_fired,
                0, 0, 0, 0,
            )
            return {
                "ok": False, "error": "blocked_by_hook",
                "hook": d.get("hook_key"),
                "reason": d.get("reason"),
            }

    # Build final system prompt
    full_system_prompt = (
        skill.system_prompt
        + "\n\n"
        + playbook_block
        + "\n\n"
        + (skill.references_md or "")
    )

    # Build user message from input_data
    user_message = _format_user_message(skill, input_data)

    # Resolve tool calling (same source as voice agent registry).
    chat_tools = _resolve_chat_tools(skill)
    tool_calls_log: list[dict[str, Any]] = []

    # Call OpenAI
    try:
        from openai import AsyncOpenAI
        import os
        client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": full_system_prompt},
            {"role": "user", "content": user_message},
        ]
        sub_ctx = {
            "firm_id": firm_id,
            "user_id": user_id,
            "matter_id": matter_id,
            "document_id": document_id,
            "subagent_chain": ["chat_skill"],
        }

        tokens_in = 0
        tokens_out = 0
        content = ""

        if not chat_tools:
            # ── Original single-shot path · backward compatible ──
            kwargs: dict[str, Any] = {
                "model": skill.model,
                "messages": messages,
                "temperature": 0.3,
                "max_tokens": MAX_TOKENS_BUDGET,
            }
            if skill.output_schema:
                kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": skill.command.replace("/", "_").strip("_"),
                        "schema": skill.output_schema,
                        "strict": False,
                    },
                }
            resp = await client.chat.completions.create(**kwargs)
            content = resp.choices[0].message.content or ""
            if resp.usage:
                tokens_in = resp.usage.prompt_tokens or 0
                tokens_out = resp.usage.completion_tokens or 0
        else:
            # ── Tool-calling loop · same pattern as agent.subagents.base ──
            # NOTE: tool calling is incompatible with response_format=json_schema
            # in many OpenAI scenarios; if a skill declares BOTH, tools win and
            # the final answer is plain text. We just skip the schema here.
            for _round in range(MAX_TOOL_ITERATIONS):
                kwargs = {
                    "model": skill.model,
                    "messages": messages,
                    "tools": chat_tools,
                    "tool_choice": "auto",
                    "temperature": 0.3,
                    "max_tokens": MAX_TOKENS_BUDGET,
                }
                resp = await client.chat.completions.create(**kwargs)
                if resp.usage:
                    tokens_in += resp.usage.prompt_tokens or 0
                    tokens_out += resp.usage.completion_tokens or 0
                choice = resp.choices[0]
                msg = choice.message

                # Always append the assistant message (with tool_calls if any).
                messages.append({
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in (msg.tool_calls or [])
                    ] if msg.tool_calls else None,
                })

                if not msg.tool_calls:
                    content = msg.content or ""
                    break

                # Execute each tool call · append tool result messages.
                for tc in msg.tool_calls:
                    fn_name = tc.function.name
                    try:
                        fn_args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        fn_args = {}
                    tool_result, _ui = await _execute_tool_call(fn_name, fn_args, skill, sub_ctx)
                    # Non-streaming path has no SSE channel for ui_command, so we
                    # drop it · only the streaming path forwards canvas/UI ops.
                    tool_calls_log.append({
                        "name": fn_name,
                        "args_keys": list(fn_args.keys()),
                        "ok": "error" not in tool_result,
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(tool_result, ensure_ascii=False, default=str),
                    })
            # If we exited the loop without a content message, get the last assistant content.
            if not content:
                content = next(
                    (m.get("content") for m in reversed(messages)
                     if m.get("role") == "assistant" and m.get("content")),
                    "",
                ) or ""
    except Exception as e:
        logger.warning("skill_runner OpenAI failed: %s", e)
        await _persist_execution(
            pool, execution_id, firm_id, user_id, skill.id, command,
            matter_id, document_id, input_data, {},
            "error", str(e)[:240], pre_hooks_fired,
            int((time.time() - started) * 1000), 0, 0, 0,
        )
        return {"ok": False, "error": "llm_failed", "detail": str(e)[:240]}

    # Parse output
    parsed_output: dict[str, Any]
    if skill.output_schema:
        try:
            parsed_output = json.loads(content)
        except json.JSONDecodeError:
            parsed_output = {"text": content, "_parse_error": True}
    else:
        parsed_output = {"text": content}

    # Post-skill hooks
    post_decisions, post_hooks_fired = await run_hooks_for_skill(
        pool, command, "post_skill",
        context={
            "input": input_data, "output": parsed_output, "playbook": playbook,
            "firm_id": firm_id, "user_id": user_id, "matter_id": matter_id,
        },
    )

    # Enriquecer output con warnings de hooks
    warnings: list[dict[str, Any]] = []
    for d in post_decisions:
        if d.get("decision") in ("warn", "block"):
            warnings.append({
                "hook": d.get("hook_key"),
                "level": d.get("decision"),
                "reason": d.get("reason"),
                "context": d.get("additional_context"),
            })
    if warnings:
        parsed_output["_warnings"] = warnings

    duration_ms = int((time.time() - started) * 1000)

    await _persist_execution(
        pool, execution_id, firm_id, user_id, skill.id, command,
        matter_id, document_id,
        {"keys": list(input_data.keys())},
        {"keys": list(parsed_output.keys()),
         "warning_count": len(warnings)},
        "success", None, pre_hooks_fired + post_hooks_fired,
        duration_ms, tokens_in, tokens_out,
        _estimate_cost_cents(tokens_in, tokens_out, skill.model),
    )

    return {
        "ok": True,
        "execution_id": execution_id,
        "command": command,
        "skill_id": skill.id,
        "is_custom": skill.is_custom,
        "duration_ms": duration_ms,
        "tokens": {"input": tokens_in, "output": tokens_out},
        "output": parsed_output,
        "warnings": warnings,
        "tool_calls": tool_calls_log,
    }


def _format_user_message(skill: SkillDefinition, input_data: dict[str, Any]) -> str:
    """Formatea input_data como user message según skill argument-hint."""
    if not input_data:
        return "(sin argumentos · usar contexto del playbook)"
    lines: list[str] = []
    if "matter_titulo" in input_data:
        lines.append(f"**Caso:** {input_data['matter_titulo']}")
    if "matter_id" in input_data:
        lines.append(f"**Matter ID:** {input_data['matter_id']}")
    if "document_text" in input_data:
        txt = input_data["document_text"]
        if len(txt) > 25000:
            txt = txt[:25000] + "\n... [truncado · doc completo en RAG]"
        lines.append(f"\n**Documento:**\n{txt}\n")
    if "prompt" in input_data:
        lines.append(f"\n**Solicitud:**\n{input_data['prompt']}")
    if "context" in input_data:
        lines.append(f"\n**Contexto adicional:**\n{input_data['context']}")
    if "args" in input_data:
        lines.append(f"\n**Argumentos:**\n```json\n{json.dumps(input_data['args'], indent=2, ensure_ascii=False)}\n```")
    if not lines:
        lines.append(f"```json\n{json.dumps(input_data, indent=2, ensure_ascii=False)[:3000]}\n```")
    return "\n".join(lines)


def _estimate_cost_cents(tokens_in: int, tokens_out: int, model: str) -> int:
    if "gpt-4o-mini" in model:
        return int((tokens_in / 1e6 * 15 + tokens_out / 1e6 * 60) * 100)
    if "gpt-4o" in model:
        return int((tokens_in / 1e6 * 250 + tokens_out / 1e6 * 1000) * 100)
    return 0


async def _persist_execution(
    pool, execution_id, firm_id, user_id, skill_id, command,
    matter_id, document_id, input_summary, output_summary,
    status, error_message, hooks_fired,
    duration_ms, tokens_in, tokens_out, cost_cents,
):
    """Insert en skill_executions audit table."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                insert into skill_executions
                  (id, firm_id, user_id, skill_id, command,
                   matter_id, document_id,
                   input_summary, output_summary,
                   status, error_message, hooks_fired,
                   duration_ms, tokens_input, tokens_output, cost_usd_cents,
                   completed_at)
                values ($1::uuid, $2::uuid, $3::uuid, $4, $5,
                        $6, $7,
                        $8::jsonb, $9::jsonb,
                        $10, $11, $12,
                        $13, $14, $15, $16,
                        case when $10 != 'running' then now() else null end)
                """,
                execution_id, firm_id, user_id,
                str(skill_id) if skill_id else None, command,
                matter_id, document_id,
                json.dumps(input_summary)[:4000],
                json.dumps(output_summary)[:4000],
                status, (error_message or "")[:240], hooks_fired,
                duration_ms, tokens_in, tokens_out, cost_cents,
            )
    except Exception as e:
        logger.warning("persist_execution failed (non-fatal): %s", e)
