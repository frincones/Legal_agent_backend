"""Tool 3 · extract_data · wrap data_extractor.extract (gpt-4o-mini)."""
from __future__ import annotations

import logging
from typing import Any, Optional

from lex.orchestrator.stages.data_extractor import extract as _extract

from .base import ToolContext, ToolDef, ToolError

logger = logging.getLogger(__name__)


class ExtractDataTool(ToolDef):
    name = "extract_data"
    description = (
        "Extrae datos estructurados del intent + brief del usuario "
        "(nombres, CC, NIT, fechas, montos, partes, etc.) según el doc_type. "
        "Devuelve extracted_fields + missing_fields. Llamar UNA vez después de "
        "load_skill_md y antes de generate_clause."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "description": "Texto del prompt del usuario en el composer"},
            "user_brief": {"type": "string", "description": "Brief adicional (opcional)"},
            "doc_type": {"type": "string"},
            "matter_id": {"type": "string", "description": "UUID del matter actual (opcional)"},
        },
        "required": ["intent", "doc_type"],
    }
    invokes_llm = True
    timeout_seconds = 30.0

    def __init__(self, openai_client=None, **_: Any):
        self.client = openai_client

    async def run(
        self,
        ctx: ToolContext,
        intent: str,
        doc_type: str,
        user_brief: Optional[str] = None,
        matter_id: Optional[str] = None,
    ) -> dict:
        client = self.client or ctx.openai_client
        if client is None:
            raise ToolError("OpenAI client no inicializado (requerido para extract_data)")
        result = await _extract(client, doc_type, intent, user_brief)
        return {
            "extracted_fields": result.extracted_fields or {},
            "missing_fields": list(result.missing_fields or []),
            "confidence": getattr(result, "confidence", None),
            "doc_type": doc_type,
        }


def build_tool(openai_client=None, **_: Any) -> ToolDef:
    return ExtractDataTool(openai_client=openai_client)
