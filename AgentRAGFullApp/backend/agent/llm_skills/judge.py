"""LLM-as-Judge primitive · multi-dimensional quality scoring.

Used by:
  - agent/tools/quality_validator.py (Sprint 3) · last filter before HITL
  - eval/skill_qa.py (Sprint 4) · scoring of golden-set generations

Tier: JUDGE (o3-mini reasoning model).

Rubric dimensions (default for Colombian legal documents):
  - completeness   · ¿están todas las secciones requeridas?
  - formality      · ¿lenguaje jurídico apropiado, evita coloquialismos?
  - persuasiveness · ¿argumentos sólidos, conclusiones se siguen de premisas?
  - compliance     · ¿cita normas vigentes, sigue CGP/CPACA forma?
  - clarity        · ¿estructura legible, sin ambigüedad?
  - faithfulness   · ¿cada afirmación se sostiene en las fuentes citadas?

Callers can override the rubric per (materia × kind).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from utils.llm_tiers import (
    Tier,
    get_tier_config,
    llm_generate_json_tier,
)

from .base import LLMSkillResult, SkillExecutionError

DEFAULT_RUBRIC: dict[str, str] = {
    "completeness": "¿Están todas las secciones requeridas para este tipo de documento?",
    "formality":    "¿Lenguaje jurídico apropiado · evita coloquialismos · trato cortés?",
    "persuasiveness": "¿Los argumentos están bien fundados · las conclusiones se siguen de las premisas?",
    "compliance":   "¿Cita normas vigentes · sigue la forma exigida por CGP/CPACA · respeta plazos?",
    "clarity":      "¿Estructura legible · oraciones claras · sin ambigüedad?",
    "faithfulness": "¿Cada afirmación factual se sostiene en las fuentes citadas · sin alucinaciones?",
}


@dataclass
class JudgeVerdict:
    """Result payload of judge_quality()."""
    overall_score: float                 # weighted average 0.0-1.0
    dimension_scores: dict[str, float]   # per-dimension 0.0-1.0
    critical_issues: list[str]           # blocking · must fix
    warnings: list[str] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    rationale: str = ""                  # one paragraph in Spanish


async def judge_quality(
    *,
    document_text: str,
    materia: str,
    doc_kind: str,
    rubric: Optional[dict[str, str]] = None,
    weights: Optional[dict[str, float]] = None,
    sources_context: Optional[str] = None,
    purpose: str = "judge",
    session_id: str = "",
) -> LLMSkillResult:
    """Score a generated legal document against a multi-dim rubric.

    Args:
        document_text: The (markdown or plain text) document to evaluate.
        materia: Practice area · informs the prompt's expertise framing.
        doc_kind: e.g. 'demanda', 'contestacion', 'tutela'.
        rubric: Optional override of dimension→question dict. Defaults to
                DEFAULT_RUBRIC (6 dimensions).
        weights: Optional override of dimension→weight (must sum to 1.0).
                 Default = equal weights.
        sources_context: Optional concat of source excerpts so the judge
                         can evaluate faithfulness against actual sources.

    Returns:
        LLMSkillResult with data=JudgeVerdict.

    Raises:
        SkillExecutionError on LLM failure.
    """
    if not document_text.strip():
        raise SkillExecutionError(
            "document_text cannot be empty",
            skill="judge_quality",
        )

    started = time.time()
    cfg = get_tier_config(Tier.JUDGE)
    rubric_eff = rubric or DEFAULT_RUBRIC

    if weights:
        if abs(sum(weights.values()) - 1.0) > 0.01:
            raise SkillExecutionError(
                f"weights must sum to 1.0, got {sum(weights.values()):.3f}",
                skill="judge_quality",
            )
        weights_eff = weights
    else:
        equal = 1.0 / len(rubric_eff)
        weights_eff = {k: equal for k in rubric_eff}

    rubric_block = "\n".join(
        f"  - {k}: {desc}  (peso: {weights_eff.get(k, 0):.2f})"
        for k, desc in rubric_eff.items()
    )

    sys_prompt = (
        f"Eres un abogado senior colombiano experto en derecho {materia} "
        f"que evalúa borradores de {doc_kind} con criterio profesional. "
        f"Tu juicio es la última barrera antes de que el documento llegue al "
        f"abogado humano. Sé honesto · marca problemas reales · no infles "
        f"scores para ser amable. Devuelve un JSON estricto."
    )

    user_prompt = (
        f"DOCUMENTO A EVALUAR ({doc_kind} · materia {materia}):\n\n"
        f"{document_text.strip()[:60000]}\n\n"
        + (
            f"FUENTES DISPONIBLES (para evaluar faithfulness):\n{sources_context[:20000]}\n\n"
            if sources_context else ""
        )
        + "RÚBRICA · evalúa cada dimensión de 0.0 a 1.0:\n"
        f"{rubric_block}\n\n"
        "Devuelve EXACTAMENTE este esquema JSON:\n"
        "{\n"
        '  "dimension_scores": {' + ", ".join(f'"{k}": <0.0-1.0>' for k in rubric_eff) + "},\n"
        '  "critical_issues": ["..."],\n'
        '  "warnings": ["..."],\n'
        '  "strengths": ["..."],\n'
        '  "rationale": "un párrafo en español"\n'
        "}\n\n"
        "Sé específico · cita secciones del documento por nombre cuando marques problemas."
    )

    try:
        raw = await llm_generate_json_tier(
            tier=Tier.JUDGE,
            prompt=user_prompt,
            system_prompt=sys_prompt,
            purpose=purpose,
            session_id=session_id,
        )
    except Exception as e:
        raise SkillExecutionError(
            f"judge LLM call failed: {e}",
            skill="judge_quality",
            cause=e,
        ) from e

    dim_scores = raw.get("dimension_scores") or {}
    # Coerce to floats · clamp to [0,1] · default missing dims to 0.5 (uncertain)
    normalized: dict[str, float] = {}
    for k in rubric_eff:
        v = dim_scores.get(k, 0.5)
        try:
            normalized[k] = max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            normalized[k] = 0.5

    overall = sum(normalized[k] * weights_eff.get(k, 0.0) for k in rubric_eff)

    verdict = JudgeVerdict(
        overall_score=round(overall, 3),
        dimension_scores={k: round(v, 3) for k, v in normalized.items()},
        critical_issues=list(raw.get("critical_issues") or []),
        warnings=list(raw.get("warnings") or []),
        strengths=list(raw.get("strengths") or []),
        rationale=str(raw.get("rationale") or ""),
    )

    duration_ms = int((time.time() - started) * 1000)
    return LLMSkillResult(
        skill="judge_quality",
        success=True,
        data=verdict,
        model=cfg.model,
        duration_ms=duration_ms,
    )
