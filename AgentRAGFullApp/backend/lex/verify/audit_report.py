"""Consolida toda la auditoría en un objeto JSON exportable."""
from __future__ import annotations

from datetime import datetime
from typing import Any


def build_audit_report(
    generation_id: str,
    template_id: str,
    duration_seconds: float,
    cost_usd: float,
    classification: dict[str, Any] | None = None,
    extraction: dict[str, Any] | None = None,
    calculations: dict[str, Any] | None = None,
    citations: list[dict] | None = None,
    citation_verifications: list[dict] | None = None,
    derogation_checks: list[dict] | None = None,
    qa_result: dict[str, Any] | None = None,
    polish_info: dict[str, Any] | None = None,
    total_blocks: int = 0,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Genera el audit_report final.

    Estructura compatible con audit_panel UI + audit.json descargable.
    """
    cit_verified = sum(1 for c in (citation_verifications or []) if c.get("verified"))
    cit_total = len(citation_verifications or [])
    norms_vigentes = sum(1 for d in (derogation_checks or []) if d.get("vigente"))
    norms_total = len(derogation_checks or [])

    citation_rate = round(cit_verified / cit_total, 3) if cit_total > 0 else None
    derogation_rate = round(norms_vigentes / norms_total, 3) if norms_total > 0 else None

    summary = {
        "citation_existence_rate": citation_rate,
        "derogation_compliance_rate": derogation_rate,
        "total_citations": cit_total,
        "verified_citations": cit_verified,
        "vigent_norms": norms_vigentes,
        "total_norms_checked": norms_total,
        "total_blocks": total_blocks,
        "duration_seconds": duration_seconds,
        "cost_usd": cost_usd,
    }

    return {
        "generation_id": generation_id,
        "template_id": template_id,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "summary": summary,
        "classification": classification or {},
        "extraction": extraction or {},
        "calculations": calculations or {},
        "citations": citations or [],
        "citation_verifications": citation_verifications or [],
        "derogation_checks": derogation_checks or [],
        "qa_result": qa_result or {},
        "polish_info": polish_info or {},
        "warnings": warnings or [],
    }
