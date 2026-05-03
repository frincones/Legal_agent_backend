"""Registry de sub-agentes + tool delegate_to expuesta al orquestador principal."""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

from .base import BaseSubAgent
from .investigador import InvestigadorSubAgent
from .redactor import RedactorSubAgent
from .calculista import CalculistaSubAgent

logger = logging.getLogger(__name__)


_SUBAGENTS: dict[str, BaseSubAgent] = {}


def _ensure_loaded() -> None:
    if _SUBAGENTS:
        return
    for cls in (InvestigadorSubAgent, RedactorSubAgent, CalculistaSubAgent):
        inst = cls()
        _SUBAGENTS[inst.name] = inst


def get_subagent(name: str) -> Optional[BaseSubAgent]:
    _ensure_loaded()
    return _SUBAGENTS.get(name)


def list_subagents() -> list[dict]:
    _ensure_loaded()
    return [
        {"name": s.name, "description": s.description, "model": s.model, "tools_count": len(s.allowed_tools)}
        for s in _SUBAGENTS.values()
    ]


# ════════════════════════════════════════════════════════════════════════
# delegate_to · tool expuesta al orquestador
# ════════════════════════════════════════════════════════════════════════


async def delegate_to_tool(args: dict, ctx: dict) -> dict:
    """Delega una tarea a un sub-agente especializado.

    args:
      subagent: 'investigador' | 'redactor' | 'calculista'
      task: descripción de la tarea (string)
      context_extra: dict opcional con campos adicionales para el sub-agente
                     (ej. matter_id explícito si difiere del activo)

    Auto-inyecta firm_id, user_id, session_id, matter_id desde ctx para que
    el sub-agente NUNCA falle por falta de contexto. Si el caller pasa
    context_extra.matter_id, ese gana sobre el del ctx.
    """
    subagent_name = (args.get("subagent") or "").strip()
    task = (args.get("task") or "").strip()
    context_extra = args.get("context_extra") or {}

    if not subagent_name or not task:
        return {"error": "subagent y task son requeridos"}

    sub = get_subagent(subagent_name)
    if sub is None:
        return {"error": f"subagent '{subagent_name}' no existe. Disponibles: {[s['name'] for s in list_subagents()]}"}

    # Auto-inyección de contexto: el sub-agente recibe firm_id/user_id/
    # matter_id/session_id heredados del orquestador. context_extra del
    # caller puede override matter_id si quiere apuntar a otro caso.
    sub_ctx = {
        "firm_id": ctx.get("firm_id"),
        "user_id": ctx.get("user_id"),
        "session_id": ctx.get("session_id"),
        "matter_id": ctx.get("matter_id"),
        "subagent_chain": list(ctx.get("subagent_chain") or []),
    }
    if isinstance(context_extra, dict):
        # Mantener None del ctx si context_extra trae valor; permitir override.
        for k, v in context_extra.items():
            if v is not None:
                sub_ctx[k] = v

    # Enriquecer la task con el contexto si está disponible: muchas veces
    # el LLM olvida pasar matter_id explícito. Si el ctx lo tiene, lo
    # añadimos como nota al final de la task.
    enriched_task = task
    if sub_ctx.get("matter_id") and "matter_id" not in task.lower():
        enriched_task = (
            f"{task}\n\n[Contexto disponible: matter_id={sub_ctx['matter_id']}, "
            f"firm_id={sub_ctx.get('firm_id')}. Úsalo si lo necesitas.]"
        )

    started = time.time()
    result = await sub.run(enriched_task, sub_ctx)
    duration_ms = int((time.time() - started) * 1000)

    # Persistir agent_subagent_run (best-effort)
    try:
        from utils.db import get_storage
        storage = await get_storage()
        if hasattr(storage, "pool"):
            async with storage.pool.acquire() as conn:
                await conn.execute(
                    """
                    insert into agent_subagent_runs
                      (firm_id, subagent_name, task, result_jsonb, tool_calls,
                       tokens_in, tokens_out, duration_ms)
                    values
                      ($1::uuid, $2, $3, $4::jsonb, $5::jsonb, $6, $7, $8)
                    """,
                    ctx.get("firm_id"),
                    subagent_name,
                    task[:1000],
                    json.dumps({k: v for k, v in result.items() if k != "tool_calls"}, default=str),
                    json.dumps(result.get("tool_calls", []), default=str),
                    int(result.get("tokens_in", 0) or 0),
                    int(result.get("tokens_out", 0) or 0),
                    duration_ms,
                )
    except Exception as e:
        logger.debug("subagent_run persist failed: %s", e)

    # Si el sub-agente devolvió un summary que parece un error
    # (ej. "no pude cargar el contexto..."), lo marcamos como error
    # para que el orquestador NO le mienta al usuario diciendo "ya hice".
    summary_text = (result.get("summary") or "").lower()
    has_error_keywords = any(k in summary_text for k in (
        "error", "no pude", "no puedo", "no logré", "necesito el", "falló",
        "no tengo acceso", "parece que hubo", "no encontré",
    ))
    if has_error_keywords and not result.get("error"):
        result["error"] = "Sub-agente reportó dificultad — revisa summary"

    # Resumen narrativo breve para el orquestador (que se lo lee al usuario)
    return {
        "subagent": subagent_name,
        "summary": result.get("summary"),
        "tool_calls_count": len(result.get("tool_calls", [])),
        "duration_ms": duration_ms,
        "error": result.get("error"),
    }
