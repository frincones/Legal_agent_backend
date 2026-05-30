"""Tool 17 · narrate_progress · emite SSE agent_thought + persiste chat_messages.

NOTA: el dispatcher convencional ejecuta la tool de forma aislada; los SSE
del Brain se emiten desde el SSE emitter (lex/brain/sse_emitter.py). Esta tool
solo registra el thought en BD y deja al emitter que lo refleje en stream.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .base import ToolContext, ToolDef

logger = logging.getLogger(__name__)


VALID_KINDS = {"tool_call", "narration", "thought", "clarification", "risk_advisory"}


class NarrateProgressTool(ToolDef):
    name = "narrate_progress"
    description = (
        "Registra un mensaje narrativo o de razonamiento del agente, visible al "
        "usuario en el chat panel. Usar para: explicar qué se va a hacer, advertir "
        "de algo importante (cita derogada, falta de datos), pedir clarificación, "
        "o cerrar resumiendo el resultado. NO usar para narración mecánica de cada "
        "tool_use (el dispatcher ya lo registra automáticamente)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "kind": {
                "type": "string",
                "enum": sorted(VALID_KINDS),
                "default": "narration",
            },
            "tool": {"type": "string", "description": "Tool relacionada (si aplica)"},
            "tool_id": {"type": "string"},
            "tool_response_summary": {"type": "string", "default": ""},
        },
        "required": ["message"],
    }
    timeout_seconds = 5.0

    def __init__(self, pool=None, **_: Any):
        self.pool = pool

    async def run(
        self,
        ctx: ToolContext,
        message: str,
        kind: str = "narration",
        tool: Optional[str] = None,
        tool_id: Optional[str] = None,
        tool_response_summary: str = "",
    ) -> dict:
        if kind not in VALID_KINDS:
            kind = "narration"

        pool = self.pool or ctx.pool
        persisted = False
        if pool is not None and ctx.firm_id is not None:
            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        insert into chat_messages (
                          thread_id, firm_id, user_id, generation_id,
                          role, channel, content, segments
                        )
                        values ($1, $2, $3, $4, 'assistant', 'chat', $5, $6::jsonb)
                        """,
                        str(ctx.generation_id),       # thread_id (mismo que generation)
                        ctx.firm_id, ctx.user_id, ctx.generation_id,
                        message,
                        json.dumps([{
                            "type": "thought" if kind == "thought" else "markdown",
                            "kind": kind,
                            "markdown": message,
                            "tool": tool,
                            "tool_id": tool_id,
                            "tool_response_summary": tool_response_summary,
                        }], default=str),
                    )
                persisted = True
            except Exception as e:
                logger.warning("narrate_progress persist failed: %s", e)

        return {
            "message": message,
            "kind": kind,
            "tool": tool,
            "tool_id": tool_id,
            "persisted": persisted,
            "_note": "El SSE agent_thought se emite desde sse_emitter, no aquí.",
        }


def build_tool(pool=None, **_: Any) -> ToolDef:
    return NarrateProgressTool(pool=pool)
