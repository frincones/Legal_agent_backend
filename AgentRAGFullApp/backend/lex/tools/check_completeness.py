"""Tool 12 · check_completeness · wrap completeness_check.check_completeness."""
from __future__ import annotations

import logging
from typing import Any, Optional

from lex.orchestrator.stages.completeness_check import check_completeness as _check

from .base import ToolContext, ToolDef, ToolError

logger = logging.getLogger(__name__)


class CheckCompletenessTool(ToolDef):
    name = "check_completeness"
    description = (
        "Verifica que el documento generado contiene todas las secciones/cláusulas "
        "requeridas por el doc_type. Retorna {ok, missing_fields, suggestions}. "
        "Invocar DESPUÉS de generar todas las cláusulas. Si falta algo crítico, "
        "el Brain debe re-llamar generate_clause con regenerate=True."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "blocks": {"type": "array", "items": {"type": "object"}},
            "doc_type": {"type": "string"},
            "run_llm_check": {"type": "boolean", "default": True},
        },
        "required": ["blocks", "doc_type"],
    }
    invokes_llm = True
    timeout_seconds = 30.0

    def __init__(self, openai_client=None, **_: Any):
        self.openai_client = openai_client

    async def run(
        self,
        ctx: ToolContext,
        blocks: list,
        doc_type: str,
        run_llm_check: bool = True,
    ) -> dict:
        client = self.openai_client or ctx.openai_client
        if client is None and run_llm_check:
            raise ToolError("check_completeness con run_llm_check=True requiere OpenAI client")

        report = await _check(
            client=client,
            blocks=blocks,
            doc_type=doc_type,
            run_llm_check=run_llm_check,
        )
        gaps = getattr(report, "gaps", [])
        missing_fields = [g.field if hasattr(g, "field") else str(g) for g in gaps
                          if hasattr(g, "severity") and g.severity == "critical"]

        return {
            "ok": getattr(report, "overall_score", 0) >= 0.7 and len(missing_fields) == 0,
            "overall_score": float(getattr(report, "overall_score", 0)),
            "rule_score": float(getattr(report, "rule_score", 0)),
            "llm_score": (float(report.llm_score) if getattr(report, "llm_score", None) is not None else None),
            "critical_count": int(getattr(report, "critical_count", 0)),
            "missing_fields": missing_fields,
            "gaps": [
                {
                    "field": getattr(g, "field", str(g)),
                    "severity": getattr(g, "severity", "info"),
                    "message": getattr(g, "message", str(g)),
                }
                for g in gaps
            ],
        }


def build_tool(openai_client=None, **_: Any) -> ToolDef:
    return CheckCompletenessTool(openai_client=openai_client)
