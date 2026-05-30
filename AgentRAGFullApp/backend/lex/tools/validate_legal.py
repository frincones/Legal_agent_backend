"""Tool 14 · validate_legal · fusiona legal_classifier + qa.

Permite al Brain validar el documento legal generado (clasificación + QA).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .base import ToolContext, ToolDef, ToolError

logger = logging.getLogger(__name__)


class ValidateLegalTool(ToolDef):
    name = "validate_legal"
    description = (
        "Valida un documento legal: clasifica el régimen aplicable, detecta riesgos "
        "legales (cláusulas abusivas, normas derogadas, requisitos faltantes) y "
        "produce advisories de mejora. Llamar al final, después de check_coherence."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "doc_type": {"type": "string"},
            "content": {"type": "string", "description": "Texto del documento (concatenado)"},
            "blocks": {"type": "array", "items": {"type": "object"}, "description": "Alternativa a content: lista de blocks"},
            "jurisdiction": {"type": "string", "default": "CO"},
        },
        "required": ["doc_type"],
    }
    invokes_llm = True
    timeout_seconds = 60.0

    def __init__(self, pool=None, openai_client=None, **_: Any):
        self.pool = pool
        self.openai_client = openai_client

    async def run(
        self,
        ctx: ToolContext,
        doc_type: str,
        content: Optional[str] = None,
        blocks: Optional[list] = None,
        jurisdiction: str = "CO",
    ) -> dict:
        client = self.openai_client or ctx.openai_client
        pool = self.pool or ctx.pool
        if client is None:
            raise ToolError("validate_legal requiere OpenAI client")

        # 1) legal_classifier (intent-based) — solo si tenemos contenido
        intent_text = content or ""
        if not intent_text and blocks:
            intent_text = self._blocks_to_text(blocks)

        classification: dict = {}
        try:
            from lex.orchestrator.stages.legal_classifier import classify_legal_case
            cls = await classify_legal_case(client, intent_text or doc_type, doc_type, pool)
            if cls:
                classification = {
                    "regimen": getattr(cls, "regimen_aplicable", None),
                    "naturaleza": getattr(cls, "naturaleza", None),
                    "fundamento": getattr(cls, "fundamento_normativo", None),
                    "premisas_corregidas": getattr(cls, "premisas_corregidas", []),
                    "advertencias": getattr(cls, "advertencias_riesgo", []),
                }
        except Exception as e:
            logger.warning("validate_legal classify_legal_case failed: %s", e)

        # 2) QA si se proveyeron blocks
        qa_result: dict = {}
        if blocks:
            try:
                from lex.orchestrator.stages.qa import run_qa
                qa_dict = await run_qa(blocks, None)   # template=None → solo lo básico
                qa_result = {
                    "passed": bool(qa_dict.get("passed", True)),
                    "score": float(qa_dict.get("score", 7.5)),
                    "issues": list(qa_dict.get("issues", [])),
                }
            except Exception as e:
                logger.warning("validate_legal run_qa failed: %s", e)

        risks = list(classification.get("advertencias", []))
        for issue in qa_result.get("issues", []):
            risks.append({"type": "qa_issue", "severity": "warning", "description": issue}
                          if isinstance(issue, str) else issue)

        return {
            "doc_type": doc_type,
            "jurisdiction": jurisdiction,
            "classification": classification,
            "qa": qa_result,
            "risks": risks,
            "advisories": classification.get("premisas_corregidas", []),
            "passed": qa_result.get("passed", True) and len(risks) == 0,
        }

    @staticmethod
    def _blocks_to_text(blocks: list) -> str:
        out: list[str] = []
        for b in blocks or []:
            if not isinstance(b, dict):
                continue
            for run in b.get("runs", []) or []:
                if isinstance(run, dict) and "text" in run:
                    out.append(run["text"])
            for k in ("text", "ratio", "contenido"):
                if isinstance(b.get(k), str):
                    out.append(b[k])
        return "\n".join(out)


def build_tool(pool=None, openai_client=None, **_: Any) -> ToolDef:
    return ValidateLegalTool(pool=pool, openai_client=openai_client)
