"""Stage 10: QA agent — checklist forense por TemplateDef.

Valida que el documento generado cumple las validation_rules del template.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def run_qa(
    all_blocks: list[dict[str, Any]],
    template,
) -> dict[str, Any]:
    """Aplica validation_rules del template y devuelve dict con passed/score/issues."""
    if not template:
        return {"passed": True, "score": 7.5, "issues": []}

    issues: list[str] = []
    sections_present = set()
    hechos_count = 0
    pretensiones_count = 0
    norma_refs: list[str] = []
    jurisp_refs: list[str] = []

    for b in all_blocks:
        bt = b.get("block_type") or b.get("block_data", {}).get("type")
        bd = b.get("block_data") or {}
        if bt == "section_heading":
            sections_present.add(bd.get("section_key", ""))
        elif bt == "hecho":
            hechos_count += 1
        elif bt == "pretension":
            pretensiones_count += 1
        elif bt == "norma_citada":
            norma_refs.append(bd.get("norma", ""))
        elif bt == "jurisprudencia":
            jurisp_refs.append(bd.get("id", ""))

    score = 10.0
    for rule in template.validation_rules:
        if rule.kind == "has_section":
            if rule.value not in sections_present:
                issues.append(f"Falta sección requerida: {rule.value}")
                score -= 1.0
        elif rule.kind == "min_hechos":
            if hechos_count < rule.value:
                issues.append(f"Hechos insuficientes: {hechos_count}/{rule.value}")
                score -= 0.5
        elif rule.kind == "min_pretensiones":
            if pretensiones_count < rule.value:
                issues.append(f"Pretensiones insuficientes: {pretensiones_count}/{rule.value}")
                score -= 0.5
        elif rule.kind == "cita_norma_minimo":
            refs = rule.value if isinstance(rule.value, list) else [rule.value]
            for ref in refs:
                if not any(ref.lower() in (n or "").lower() for n in norma_refs):
                    issues.append(f"Cita faltante (norma): {ref}")
                    score -= 0.3
        elif rule.kind == "cita_jurisprudencia_minimo":
            if len(jurisp_refs) < (rule.value or 1):
                issues.append(f"Jurisprudencia insuficiente: {len(jurisp_refs)}/{rule.value}")
                score -= 0.3

    # Detail profile checks
    dp = template.detail_profile
    if dp.require_juramento and "juramento" not in sections_present:
        issues.append("Falta juramento (require_juramento=True)")
        score -= 0.5
    if dp.jurisprudencia_min > 0 and len(jurisp_refs) < dp.jurisprudencia_min:
        issues.append(f"Jurisprudencia mínima: {len(jurisp_refs)}/{dp.jurisprudencia_min}")
        score -= 0.3

    score = max(0.0, min(10.0, score))
    return {
        "passed": len(issues) == 0,
        "score": round(score, 2),
        "issues": issues,
        "model": "rule-based-qa",
        "checks": {
            "sections_present": list(sections_present),
            "hechos_count": hechos_count,
            "pretensiones_count": pretensiones_count,
            "norma_refs_count": len(norma_refs),
            "jurisp_refs_count": len(jurisp_refs),
        },
    }
