"""Tool 13 · check_coherence · wrap coherence_check.check_coherence."""
from __future__ import annotations

import logging
from typing import Any

from lex.orchestrator.stages.coherence_check import check_coherence as _check

from .base import ToolContext, ToolDef, ToolError

logger = logging.getLogger(__name__)


class CheckCoherenceTool(ToolDef):
    name = "check_coherence"
    description = (
        "Verifica coherencia interna del documento generado (mismas partes, mismos "
        "montos, mismas fechas, cross-references válidas). Aplica 6 gates de "
        "coherencia con LLM-as-judge. Llamar después de generar todas las cláusulas "
        "y antes de build_docx."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "blocks": {"type": "array", "items": {"type": "object"}},
            "doc_type": {"type": "string"},
        },
        "required": ["blocks"],
    }
    invokes_llm = True
    timeout_seconds = 45.0

    def __init__(self, openai_client=None, **_: Any):
        self.openai_client = openai_client

    async def run(
        self,
        ctx: ToolContext,
        blocks: list,
        doc_type: str = "",
    ) -> dict:
        client = self.openai_client or ctx.openai_client
        if client is None:
            raise ToolError("check_coherence requiere OpenAI client")

        report = await _check(client=client, blocks=blocks, doc_type=doc_type or None)

        passed = list(getattr(report, "passed", []) or [])
        failed = list(getattr(report, "failed", []) or [])

        return {
            "ok": len(failed) == 0,
            "overall_score": float(getattr(report, "overall_score", 0)),
            "passed_gates": passed,
            "failed_gates": [
                {
                    "gate": getattr(f, "gate", "unknown"),
                    "issue": getattr(f, "issue", str(f)),
                    "severity": getattr(f, "severity", "warning"),
                    "block_ids": getattr(f, "block_ids", []),
                }
                for f in failed
            ],
            "failed_count": len(failed),
        }


def build_tool(openai_client=None, **_: Any) -> ToolDef:
    return CheckCoherenceTool(openai_client=openai_client)
