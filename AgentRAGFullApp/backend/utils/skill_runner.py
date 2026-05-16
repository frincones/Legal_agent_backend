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

    try:
        from openai import AsyncOpenAI
        import os
        client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

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
        # Streaming NO soporta structured json_schema response_format; si el skill
        # lo requiere, el cliente debe llamar a /execute en su lugar. Aquí
        # forzamos texto libre.

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

    # Call OpenAI
    try:
        from openai import AsyncOpenAI
        import os
        client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        kwargs: dict[str, Any] = {
            "model": skill.model,
            "messages": [
                {"role": "system", "content": full_system_prompt},
                {"role": "user", "content": user_message},
            ],
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
        tokens_in = resp.usage.prompt_tokens if resp.usage else 0
        tokens_out = resp.usage.completion_tokens if resp.usage else 0
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
