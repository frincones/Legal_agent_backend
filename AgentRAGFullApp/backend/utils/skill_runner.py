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

    Auto-generates a minimal descriptor (name + generic description + open
    parameters) for tools that are in allowed-tools + _tool_registry but
    don't have an explicit descriptor in voice.py. Sin esto, el chat agent
    no podía llamar create_task, predict_outcome, track_time, extract_lesson,
    get_judge_stats, add_comment, log_expense, generate_invoice, etc. —
    estaban registradas en main.py pero invisibles para el LLM porque no
    se les escribió descriptor manual en voice.py.

    Returns [] when the skill doesn't declare any tools.
    """
    allowed = skill.allowed_tools
    if not allowed:
        return []
    try:
        from api.voice import _tool_descriptors, _tool_registry
        descriptors = _tool_descriptors()
        registry_names = set(_tool_registry.keys())
    except Exception as e:
        logger.warning("skill_runner could not load _tool_descriptors: %s", e)
        return []

    descriptor_by_name = {d.get("name"): d for d in descriptors if d.get("name")}

    if "*" in allowed:
        names_to_emit = registry_names
    else:
        names_to_emit = set(allowed) & registry_names

    out: list[dict[str, Any]] = []
    for name in sorted(names_to_emit):
        if name in descriptor_by_name:
            out.append({"type": "function", "function": descriptor_by_name[name]})
        else:
            # Auto-generated minimal descriptor · permisivo en parameters
            # para que el LLM pueda pasar lo que necesite. La tool sabe
            # validar sus propios args.
            out.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": _auto_description(name),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": True,
                    },
                },
            })
    return out


def _auto_description(tool_name: str) -> str:
    """Genera una descripción mínima útil para un tool sin descriptor manual.

    No reemplaza a un descriptor bien escrito · solo evita que el LLM ignore
    la tool por completo. Si el tool es importante, conviene escribir el
    descriptor real en api/voice.py.
    """
    # Mapping mínimo de tools comunes para que el LLM las elija bien.
    hints = {
        "predict_outcome": "Predice el resultado del caso e identifica riesgos. Usa cuando el usuario diga 'predice', 'pronostica', 'identifica riesgos'.",
        "extract_lesson": "Extrae lecciones aprendidas del caso. Usa cuando el usuario diga 'extrae lecciones', 'lecciones aprendidas'.",
        "create_task": "Crea una tarea (tabla tasks) asignada al usuario por default. Usa cuando el usuario diga 'crea/agrega/nueva tarea'.",
        "complete_task": "Marca una tarea como completada.",
        "track_time": "Registra tiempo facturable en un caso (matter_id, minutes). Usa cuando el usuario diga 'registra X minutos/horas'.",
        "log_expense": "Registra un gasto facturable. Usa cuando el usuario diga 'registra gasto'.",
        "generate_invoice": "Genera factura del caso a partir de horas/gastos. Usa cuando el usuario diga 'genera/crea factura'.",
        "get_judge_stats": "Devuelve estadísticas históricas del juez (winrate por materia, etc.).",
        "search_judge": "Busca jueces por nombre/sala/especialidad.",
        "simulate_judge_view": "Simula cómo el juez asignado vería este caso.",
        "validate_identity": "Verifica identidad de una persona/empresa (cédula/NIT) contra fuentes oficiales.",
        "check_doc_consistency": "Detecta inconsistencias internas en un documento.",
        "score_evidence": "Calcula puntaje probatorio de un documento.",
        "add_comment": "Agrega un comentario colaborativo al caso/documento. Soporta menciones @user.",
        "resolve_comment": "Marca un hilo de comentarios como resuelto.",
        "what_today": "Devuelve dashboard del día (tareas, plazos, menciones).",
        "what_is_my_priority": "Devuelve la prioridad #1 del usuario ahora.",
        "show_activity": "Lista feed de actividad reciente del caso/firma.",
        "show_active_users": "Lista usuarios activos en el caso ahora.",
        "search_kb": "Busca en la base de conocimiento del despacho (knowledge_entries).",
        "search_lessons": "Busca lecciones aprendidas similares.",
        "add_to_kb": "Crea entrada en la knowledge base del despacho.",
        "remember": "Guarda un dato en memoria persistente del agente.",
        "recall": "Recupera un dato guardado por key exacto.",
        "recall_relevant": "Recupera datos guardados similares al query.",
        "forget": "Elimina un dato de memoria.",
        "subscribe_to_expediente": "Suscribe a notificaciones de un expediente judicial.",
        "list_judicial_notifications": "Lista notificaciones judiciales.",
        "poll_judicial_now": "Fuerza poll inmediato de novedades judiciales.",
        "sync_calendar": "Sincroniza calendario con Google/Outlook.",
        "sync_email_now": "Sincroniza email con la bandeja del despacho.",
        "parse_legal_email": "Clasifica un email como legal/no-legal y extrae metadata.",
        "daily_briefing": "Genera resumen del día con plazos + judiciales + emails.",
        "run_sla_reminders": "Dispara recordatorios SLA configurados.",
        "record_trust_deposit": "Registra depósito en cuenta fiduciaria.",
        "record_trust_payment": "Registra pago desde cuenta fiduciaria.",
        "check_trust_balance": "Consulta balance de cuenta fiduciaria por caso.",
        "review_contract": "Ejecuta /revisar/contrato sobre un documento.",
        "apply_redline": "Aplica redlines aceptados al texto del documento.",
        "reject_redline": "Rechaza redlines preservando el texto original.",
        "list_wizards": "Lista wizards de intake disponibles.",
        "start_wizard": "Inicia una sesión de wizard de intake.",
        "wizard_session_status": "Consulta estado de una sesión de wizard.",
        "capture_lead": "Captura un lead nuevo en el pipeline (CRM).",
        "run_automation": "Ejecuta una regla de automatización.",
        "generate_insights": "Genera insights proactivos de la firma.",
        "send_whatsapp": "Envía mensaje WhatsApp via Graph API.",
        "send_for_signature": "Envía documento a firma digital (DocuSign).",
        "check_signature_status": "Consulta estado de envelope de firma.",
        "import_csv": "Procesa job de import CSV.",
        "find_anything": "Búsqueda global FTS en todos los recursos del despacho.",
        "set_matter_priority": "Cambia prioridad del caso (baja/media/alta/critica/urgente).",
        "tag_matter": "Añade un tag de 1-3 palabras al caso.",
        "update_matter_etapa": "Cambia la etapa procesal del caso.",
        "archive_matter": "Archiva un caso (soft delete).",
        "create_matter": "Crea un nuevo caso/expediente.",
        "request_human_approval": "Pide aprobación humana para una acción crítica.",
        "list_pending_hitl": "Lista interrupts HITL pendientes.",
        "delegate_to": "Delega tarea compleja a un sub-agente especializado.",
        "execute_skill": "Ejecuta una skill personalizada del despacho.",
        "extract_variables_from_text": "Extrae variables estructuradas de un texto.",
        "autofill_template": "Auto-rellena una plantilla con datos del caso.",
        "list_intake_forms": "Lista formularios de intake del despacho.",
        "list_new_submissions": "Lista submissions nuevos de intake.",
    }
    return hints.get(
        tool_name,
        f"Tool {tool_name} · ver agent/tools para detalles. Acepta argumentos del caso (matter_id, etc.).",
    )


async def _execute_tool_call(
    fn_name: str,
    fn_args: dict[str, Any],
    skill: SkillDefinition,
    ctx: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run one tool from the global _tool_registry.

    Returns (llm_result, ui_commands):
      - llm_result: dict sent back to the model (with _ui_command/_ui_commands
        stripped so the LLM doesn't see implementation noise).
      - ui_commands: lista (posiblemente vacía) de payloads {action,...} a
        reenviar al frontend como eventos SSE `ui_command` separados. Una
        tool normal emite 0 o 1; `delegate_to` puede emitir varios cuando
        el sub-agente ejecutó múltiples canvas/data_changed tools.
    """
    try:
        from api.voice import _tool_registry
    except Exception as e:
        return {"error": f"tool_registry unavailable: {e}"}, []

    allowed = set(skill.allowed_tools)
    is_wildcard = "*" in allowed
    if not is_wildcard and fn_name not in allowed:
        return {"error": f"tool '{fn_name}' not allowed for skill {skill.command}"}, []

    fn = _tool_registry.get(fn_name)
    if fn is None:
        return {"error": f"tool '{fn_name}' not registered"}, []

    try:
        result = await fn(args=fn_args, ctx=ctx)
    except Exception as e:
        logger.exception("skill_runner tool %s raised: %s", fn_name, e)
        return {"error": str(e)[:240]}, []

    ui_commands: list[dict[str, Any]] = []
    if isinstance(result, dict):
        if "_ui_commands" in result:
            collected = result.get("_ui_commands") or []
            if isinstance(collected, list):
                ui_commands.extend([c for c in collected if c])
            result = {
                k: v for k, v in result.items()
                if k not in ("_ui_command", "_ui_commands")
            }
        elif "_ui_command" in result:
            single = result.get("_ui_command")
            if single:
                ui_commands.append(single)
            result = {k: v for k, v in result.items() if k != "_ui_command"}
    return result, ui_commands


async def run_skill_stream(
    pool,
    *,
    firm_id: str,
    user_id: str,
    command: str,
    input_data: dict[str, Any],
    matter_id: Optional[str] = None,
    document_id: Optional[str] = None,
    history: Optional[list[dict[str, Any]]] = None,
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
    # Inyecta matter_id + document_id en input_data ANTES de formatear para
    # que el LLM los vea como parte del user_message · sin esto solo viven
    # en ctx (que las tools ven, pero el LLM no). Permite que `add_matter_note`
    # se llame directo sin perder ciclos buscando el caso.
    if matter_id and not input_data.get("matter_id"):
        input_data = {**input_data, "matter_id": matter_id}
    if document_id and not input_data.get("document_id"):
        input_data = {**input_data, "document_id": document_id}
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
            # Conversation memory · prepend últimos N turnos (user/assistant)
            # entre el system prompt y el user_message actual. Permite que el
            # agente recuerde lo que pidió ('necesito el nombre del cliente')
            # y use la respuesta del usuario en el siguiente turno.
            history_msgs: list[dict[str, Any]] = []
            if history:
                for h in history[-12:]:  # cap por si llega historia larga
                    role = h.get("role")
                    content = h.get("content")
                    if role in ("user", "assistant") and content:
                        history_msgs.append({"role": role, "content": str(content)[:4000]})
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": full_system_prompt},
                *history_msgs,
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
                # Pass user prompt for tools that need to parse args when the
                # LLM didn't extract them explicitly (amounts, ids, body).
                "user_prompt": input_data.get("prompt"),
            }
            # Inyecta UI context (current_path, active_tab, canvas_has_content)
            # cuando el frontend lo pasa · permite que tools y prompt sepan en
            # qué pestaña está el usuario y eviten confusión notas vs canvas.
            ui_ctx = input_data.get("context")
            if isinstance(ui_ctx, dict):
                for k in ("current_path", "active_tab", "canvas_has_content",
                          "canvas_chars"):
                    if k in ui_ctx and ui_ctx[k] is not None:
                        sub_ctx[k] = ui_ctx[k]
            # Detección de intent forzado · si el user_prompt tiene señales
            # claras de "agregar nota al caso" y add_matter_note está
            # disponible, en el primer round forzamos esa tool con
            # tool_choice. Esto vence la confusión del LLM con tag_matter,
            # canvas_append, open_matter_context, etc.
            avail_names = {t["function"]["name"] for t in chat_tools
                           if isinstance(t, dict) and t.get("function")}
            forced_tool_first_round: Optional[str] = _detect_forced_tool(
                input_data.get("prompt") or "",
                (input_data.get("context") or {}).get("active_tab"),
                avail_names,
            )
            logger.info(
                "skill_runner force_check · prompt=%r active_tab=%r forced=%r "
                "add_matter_note_in_avail=%s",
                (input_data.get("prompt") or "")[:100],
                (input_data.get("context") or {}).get("active_tab"),
                forced_tool_first_round,
                "add_matter_note" in avail_names,
            )
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
                # Forza la tool en el primer round si detect_forced_tool acertó.
                if round_idx == 0 and forced_tool_first_round:
                    kwargs["tool_choice"] = {
                        "type": "function",
                        "function": {"name": forced_tool_first_round},
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
                    tool_result, ui_commands = await _execute_tool_call(
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
                    # Reenvía CADA ui_command como evento SSE separado · el
                    # frontend handler los dispatcha en orden. Esto cubre el
                    # caso de delegate_to que acumula varios del sub-agente.
                    for ui_command in ui_commands:
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
    history: Optional[list[dict[str, Any]]] = None,
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

    # Inyecta matter_id + document_id como en run_skill_stream · ver
    # comentario allá. Permite al LLM verlos en el user_message.
    if matter_id and not input_data.get("matter_id"):
        input_data = {**input_data, "matter_id": matter_id}
    if document_id and not input_data.get("document_id"):
        input_data = {**input_data, "document_id": document_id}
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

        history_msgs: list[dict[str, Any]] = []
        if history:
            for h in history[-12:]:
                role = h.get("role")
                content = h.get("content")
                if role in ("user", "assistant") and content:
                    history_msgs.append({"role": role, "content": str(content)[:4000]})
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": full_system_prompt},
            *history_msgs,
            {"role": "user", "content": user_message},
        ]
        sub_ctx = {
            "firm_id": firm_id,
            "user_id": user_id,
            "matter_id": matter_id,
            "document_id": document_id,
            "subagent_chain": ["chat_skill"],
            "document_text": input_data.get("document_text"),
            "user_prompt": input_data.get("prompt"),
        }
        ui_ctx = input_data.get("context")
        if isinstance(ui_ctx, dict):
            for k in ("current_path", "active_tab", "canvas_has_content",
                      "canvas_chars"):
                if k in ui_ctx and ui_ctx[k] is not None:
                    sub_ctx[k] = ui_ctx[k]

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
                    # Non-streaming path has no SSE channel · _ui se descarta
                    # (los callers de run_skill no-stream son admin/test paths
                    # que no necesitan reflejo visual en el navegador).
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


def _detect_forced_tool(
    prompt: str,
    active_tab: Optional[str],
    available: set[str],
) -> Optional[str]:
    """Detecta intent del usuario y devuelve el nombre del tool que el LLM
    DEBE ejecutar en el primer round. Solo dispara cuando la señal es muy
    clara y la tool está en `available`. Devuelve None si no aplica.

    Cubre 16 escenarios (uno por pestaña del caso) · cuando el LLM tiene 75+
    tools disponibles se confunde y elige tools de lookup en vez de la write
    tool correcta. Este detector vence ese problema cuando el prompt tiene
    señales claras.

    Orden de evaluación importa · si el prompt menciona "documento/canvas"
    no fuerza tools de write-back; deja que el LLM elija canvas_*.
    """
    if not prompt:
        return None
    low = prompt.lower()
    # DEBUG · versión del detector (bump cuando edites para verificar deploy).
    logger.info("_detect_forced_tool v2026-05-18-r3 · prompt_lower=%r tab=%r",
                low[:80], active_tab)

    # Anti-canvas guard: solo bloquea cuando la intención es claramente
    # ESCRIBIR al documento del canvas. Frases como "extrae las partes DEL
    # documento" o "analiza DEL documento" NO son intent de canvas, son
    # intent de análisis · no deben bloquear.
    import re
    canvas_write_patterns = (
        r"\bagrega.*al documento\b",
        r"\bañade\s+al\s+documento\b",
        r"\binserta\s+(en|al)\s+(el\s+)?documento\b",
        r"\bredacta\s+el\s+documento\b",
        r"\bescribe\s+en\s+(el\s+)?documento\b",
        r"\breemplaza\s+(el\s+)?documento\b",
        r"\bborrador\s+del\s+documento\b",
    )
    for pat in canvas_write_patterns:
        if re.search(pat, low):
            return None
    if active_tab == "canvas":
        return None  # usuario está editando · respeta su intención

    # Lista ordenada por especificidad · primer match gana.
    # (substring or regex, forced_tool_name)
    intents: list[tuple[str, str]] = [
        # --- Tareas (specific) · "tarea:" es señal fuerte
        (r"\b(crea|crear|agrega|agregar|nueva)\s+(una\s+)?tarea\b", "create_task"),
        (r"\btarea:\s", "create_task"),
        (r"\bpreparar\b.*\bpara el\b", "create_task"),
        (r"\bnueva\s+tarea\b", "create_task"),
        # --- Plazos / calendario (specific)
        (r"\bagend(a|ar)\b", "add_matter_deadline"),
        (r"\bagenda\s+audiencia\b", "add_matter_deadline"),
        (r"\baudiencia\s+(el|para)\b", "add_matter_deadline"),
        (r"\b(agrega|crea)\s+(un\s+)?plazo\b", "add_matter_deadline"),
        (r"\b(agrega|crea)\s+(un\s+)?evento\b", "add_matter_deadline"),
        (r"\b(crear|añadir).*deadline\b", "add_matter_deadline"),
        # --- Horas/gastos
        (r"\bregistra\b.*\b(minutos|horas|hrs|min)\b", "track_time"),
        (r"\btrackea\b.*\bhoras\b", "track_time"),
        (r"\b(registra|anota|agrega)\s+gasto\b", "log_expense"),
        (r"\b(genera|crea)\s+factura\b", "generate_invoice"),
        # --- "Ver detalle/contexto del caso" → open_matter_context
        (r"\bmu[ée]strame\s+(el\s+)?(detalle|caso|expediente|contexto)\b", "open_matter_context"),
        (r"\b(ver|veamos)\s+(el\s+)?(detalle|expediente|caso)\b", "open_matter_context"),
        (r"\bdetalle\s+del\s+caso\b", "open_matter_context"),
        (r"\babre\s+(el\s+)?caso\b", "open_matter_context"),
        # --- Marcar deadline como completado
        (r"\bmarca\s+como\s+completad[ao]\s+.{0,40}\b(audiencia|plazo|deadline|conciliaci[óo]n)\b", "mark_deadline_done"),
        (r"\bcompletar?\s+(la\s+|el\s+)?(audiencia|plazo)\b", "mark_deadline_done"),
        (r"\b(audiencia|plazo)\s+(ya\s+)?(cumplid[ao]|hech[ao]|complet[ao]|realizad[ao])\b", "mark_deadline_done"),
        # --- Completar tarea
        (r"\bmarca\s+como\s+completad[ao]\s+.{0,40}\btarea\b", "complete_task"),
        (r"\bcomplet(a|ar)\s+(la\s+)?tarea\b", "complete_task"),
        # --- Resolver comentario
        (r"\b(resuelve|marca\s+como\s+resuelto)\s+(el\s+)?comentario\b", "resolve_comment"),
        # --- Predicción / riesgos / refundamentación
        (r"\bpredice\b", "predict_outcome"),
        (r"\bpredicción\b", "predict_outcome"),
        (r"\bidentifica\s+(los\s+)?riesgos\b", "predict_outcome"),
        (r"\briesgos\s+principales\b", "predict_outcome"),
        (r"\brefundamentaci[óo]n\b", "predict_outcome"),
        (r"\b(sugiere|sugerir)\s+(la\s+)?(estrategia|tesis|argumentos)\b", "predict_outcome"),
        # --- Lecciones
        (r"\bextrae\s+lecciones\b", "extract_lesson"),
        (r"\blecciones\s+aprendidas\b", "extract_lesson"),
        # --- Análisis / documentos
        (r"\bextrae\s+(las\s+)?partes\b", "extract_document_entities"),
        (r"\bextrae\s+entidades\b", "extract_document_entities"),
        (r"\banaliza\s+(este\s+)?contrato\b", "analyze_contract"),
        (r"\banaliza\s+(este\s+)?documento\b", "extract_document_entities"),
        # --- Evidencia
        (r"\bverifica\s+(la\s+)?identidad\b", "validate_identity"),
        (r"\bvalida\s+(la\s+)?cédula\b", "validate_identity"),
        (r"\bvalida\s+(la\s+)?identidad\b", "validate_identity"),
        (r"\bvalida\s+nit\b", "validate_identity"),
        (r"\bverifica\s+consistencia\b", "check_doc_consistency"),
        (r"\binconsistencias?\s+(internas?\s+|del\s+)?(documento)?\b", "check_doc_consistency"),
        (r"\bscore\s+(probator(io|ia)|de\s+evidencia)\b", "score_evidence"),
        (r"\bcalcula\s+el\s+score\b", "score_evidence"),
        (r"\beval[úu]a\s+(la\s+)?evidencia\b", "score_evidence"),
        # --- Juez
        (r"\bestadísticas\s+del\s+juez\b", "get_judge_stats"),
        (r"\bperfil\s+del\s+juez\b", "search_judge"),
        # --- Citas / normas
        (r"\bverifica\s+(las\s+)?citas?\b", "validate_citation"),
        (r"\bvalida\s+(las\s+)?citas?\b", "validate_citation"),
        (r"\b(está\s+)?derogada\b", "validate_norm_vigencia"),
        # --- Comentarios
        (r"\b(agrega|añade|crea)\s+(un\s+)?comentario\b", "add_comment"),
        (r"\bcomentario:\s", "add_comment"),
        # --- Drafting
        (r"\bredacta\s+(una|la)\s+(contestación|demanda|tutela|escrito)\b", "draft_pleading"),
        (r"\bborrador\s+de\s+(contestación|demanda)\b", "draft_pleading"),
        # --- Notas (último para que tareas/comentarios ganen si son más específicos)
        (r"\b(agrega|crea|añade|anade)\s+(una\s+)?nota\b", "add_matter_note"),
        (r"\b(agrega|crea|añade)\s+(unas\s+)?notas\b", "add_matter_note"),
        (r"\banota\s+", "add_matter_note"),
        (r"\banotar\s+", "add_matter_note"),
        (r"\banotación\b", "add_matter_note"),
        (r"\bpasos\s+pendientes\b", "add_matter_note"),
        (r"\brecordatorio:\s", "add_matter_note"),
        (r"\bobservación:\s", "add_matter_note"),
        (r"\bdentro\s+del\s+caso\b", "add_matter_note"),
        (r"\bal\s+expediente\b", "add_matter_note"),
    ]
    for pattern, tool in intents:
        if re.search(pattern, low):
            logger.info("_detect_forced_tool MATCH pattern=%r tool=%r in_avail=%s",
                        pattern, tool, tool in available)
            if tool in available:
                return tool
    return None


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

    # Intent-detection hint · forza al LLM a usar add_matter_note cuando el
    # prompt tiene señales fuertes de "agregar nota al caso" (no al documento).
    # Sin esto, gpt-4o se confunde y elige tag_matter, canvas_append, o
    # ui_open_matter_tab. El hint sale al final para que sea lo último que el
    # modelo lea antes de decidir tools.
    prompt_raw = (input_data.get("prompt") or "").lower()
    has_matter_id = bool(input_data.get("matter_id"))
    if prompt_raw and has_matter_id:
        canvas_words = ("documento", "demanda", "contestación", "contestacion",
                        "contrato", "borrador", "redacta", "redactar", "escrito",
                        "cláusula", "clausula")
        note_triggers = (
            "agrega una nota", "agregar una nota", "agrega nota",
            "crea una nota", "crear una nota", "crea nota",
            "añade una nota", "anade una nota", "añade nota",
            "anota ", "anotar ", "anotación", "anotacion",
            "agrega notas", "crea notas", "añade notas",
            "pasos pendientes", "recordatorio", "observación",
            "observacion del caso", "dentro del caso", "al expediente",
            "en el expediente",
        )
        in_canvas_tab = (input_data.get("context") or {}).get("active_tab") == "canvas"
        looks_like_note = any(t in prompt_raw for t in note_triggers)
        looks_like_canvas = any(w in prompt_raw for w in canvas_words)
        if looks_like_note and not looks_like_canvas and not in_canvas_tab:
            lines.append(
                "\n---\n"
                "**🎯 INTENT DETECTADO · ALTA CONFIANZA:** el usuario quiere "
                "AGREGAR UNA NOTA al expediente (módulo Notas, no el documento "
                "del canvas).\n\n"
                "**Acción obligatoria:** llama `add_matter_note(matter_id, body)` "
                "DIRECTAMENTE con el matter_id de arriba. NO llames "
                "`tag_matter`, `canvas_append`, `canvas_set_text`, "
                "`ui_open_matter_tab`, `ui_open_command_palette`, "
                "`open_matter_context`, `list_my_matters`, "
                "`list_upcoming_deadlines` ni ninguna otra tool de lookup. "
                "Si no estás seguro del body exacto, infiérelo del prompt del "
                "usuario y ejecuta · luego confirma brevemente en 1 línea."
            )
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
