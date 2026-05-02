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
    """
    subagent_name = (args.get("subagent") or "").strip()
    task = (args.get("task") or "").strip()
    context_extra = args.get("context_extra") or {}

    if not subagent_name or not task:
        return {"error": "subagent y task son requeridos"}

    sub = get_subagent(subagent_name)
    if sub is None:
        return {"error": f"subagent '{subagent_name}' no existe. Disponibles: {[s['name'] for s in list_subagents()]}"}

    sub_ctx = {**ctx}
    if isinstance(context_extra, dict):
        sub_ctx.update(context_extra)

    started = time.time()
    result = await sub.run(task, sub_ctx)
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

    # Resumen narrativo breve para el orquestador (que se lo lee al usuario)
    return {
        "subagent": subagent_name,
        "summary": result.get("summary"),
        "tool_calls_count": len(result.get("tool_calls", [])),
        "duration_ms": duration_ms,
        "error": result.get("error"),
    }
