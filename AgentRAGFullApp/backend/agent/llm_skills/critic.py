"""LLM-as-Critic primitive · section-by-section review with fix suggestions.

The Critic differs from the Judge:
  - Judge scores the WHOLE document on multiple dimensions (gate before HITL)
  - Critic reviews ONE section and proposes SPECIFIC fixes (inside the loop)

Used by:
  - agent/workers/document_generator.py (Sprint 3) · between DRAFT and EDIT
    stages of the LangGraph multi-agent · each section's draft goes through
    the critic and the editor applies the suggested fixes before moving on

Tier: JUDGE (o3-mini · reasoning helps catch subtle legal errors).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from utils.llm_tiers import (
    Tier,
    get_tier_config,
    llm_generate_json_tier,
)

from .base import LLMSkillResult, SkillExecutionError

Severity = str  # 'critical' | 'warning' | 'suggestion'


@dataclass
class CritiqueFinding:
    """Single issue found by the critic, with a concrete fix suggestion."""
    severity: Severity
    issue: str                          # what's wrong (1-2 sentences)
    suggested_fix: str                  # how to fix it (concrete text or rule)
    norm_reference: Optional[str] = None  # e.g. "CGP art. 82"
    location_hint: Optional[str] = None   # quoted snippet or paragraph #


@dataclass
class CritiqueResult:
    """Aggregated critic output for one section."""
    section_name: str
    section_score: float                # 0.0-1.0
    findings: list[CritiqueFinding]
    overall_assessment: str             # 1-2 sentence summary in Spanish


async def critique_section(
    *,
    section_name: str,
    section_text: str,
    materia: str,
    doc_kind: str,
    checklist_yaml: Optional[str] = None,
    style_guide_md: Optional[str] = None,
    playbook_md: Optional[str] = None,
    purpose: str = "critic",
    session_id: str = "",
) -> LLMSkillResult:
    """Critique a single section of a generated document.

    Args:
        section_name: e.g. 'Hechos', 'Pretensiones', 'Fundamentos'.
        section_text: The drafted section content.
        materia: Practice area for prompt framing.
        doc_kind: e.g. 'demanda', 'contestacion', 'tutela'.
        checklist_yaml: Optional YAML rules (template_checklists row) the
                        critic must enforce in this section.
        style_guide_md: Optional materia overlay (Sprint 4).
        playbook_md: Optional firm_playbook.raw_md for house style.

    Returns:
        LLMSkillResult with data=CritiqueResult.
    """
    if not section_text.strip():
        raise SkillExecutionError(
            "section_text cannot be empty",
            skill="critique_section",
        )

    started = time.time()
    cfg = get_tier_config(Tier.JUDGE)

    sys_prompt = (
        f"Eres un abogado senior colombiano experto en {materia}. Revisas un "
        f"borrador de la sección '{section_name}' de un(a) {doc_kind}. "
        "Tu tarea es identificar QUÉ está mal y SUGERIR cómo arreglarlo · no "
        "reescribas la sección entera · señala issues concretas. Sé estricto "
        "pero constructivo. Cada finding debe ser accionable: el editor debe "
        "poder aplicar tu sugerencia mecánicamente."
    )

    context_parts: list[str] = []
    if checklist_yaml:
        context_parts.append("CHECKLIST OBLIGATORIO:\n" + checklist_yaml.strip()[:4000])
    if style_guide_md:
        context_parts.append("GUÍA DE ESTILO:\n" + style_guide_md.strip()[:4000])
    if playbook_md:
        context_parts.append("PLAYBOOK DEL DESPACHO:\n" + playbook_md.strip()[:3000])

    context_block = "\n\n".join(context_parts) if context_parts else "(sin reglas adicionales)"

    user_prompt = (
        f"SECCIÓN A REVISAR · '{section_name}':\n\n"
        f"{section_text.strip()[:30000]}\n\n"
        "---\n\n"
        f"{context_block}\n\n"
        "---\n\n"
        "Devuelve un JSON con este esquema EXACTO:\n"
        "{\n"
        '  "section_score": <0.0-1.0>,\n'
        '  "overall_assessment": "1-2 oraciones en español",\n'
        '  "findings": [\n'
        "    {\n"
        '      "severity": "critical" | "warning" | "suggestion",\n'
        '      "issue": "qué está mal (1-2 oraciones)",\n'
        '      "suggested_fix": "cómo arreglarlo (texto o regla concreta)",\n'
        '      "norm_reference": "opcional · norma aplicable",\n'
        '      "location_hint": "opcional · cita corta del texto a cambiar"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Si no encuentras issues, devuelve findings vacío y score alto."
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
            f"critic LLM call failed: {e}",
            skill="critique_section",
            cause=e,
        ) from e

    findings_raw = raw.get("findings") or []
    findings: list[CritiqueFinding] = []
    for f in findings_raw:
        sev = (f.get("severity") or "suggestion").lower()
        if sev not in ("critical", "warning", "suggestion"):
            sev = "suggestion"
        findings.append(
            CritiqueFinding(
                severity=sev,
                issue=str(f.get("issue") or ""),
                suggested_fix=str(f.get("suggested_fix") or ""),
                norm_reference=f.get("norm_reference"),
                location_hint=f.get("location_hint"),
            )
        )

    try:
        score = max(0.0, min(1.0, float(raw.get("section_score", 0.5))))
    except (TypeError, ValueError):
        score = 0.5

    result = CritiqueResult(
        section_name=section_name,
        section_score=round(score, 3),
        findings=findings,
        overall_assessment=str(raw.get("overall_assessment") or ""),
    )

    duration_ms = int((time.time() - started) * 1000)
    return LLMSkillResult(
        skill="critique_section",
        success=True,
        data=result,
        model=cfg.model,
        duration_ms=duration_ms,
    )
