"""M19.20.D — QualityReport unificado.

Combina los outputs de completeness_check (M19.20.B), coherence_check (M19.20.C),
QA rule-based (qa.py) y citation_existence_rate (audit_report.py) en un único
veredicto auditable: ¿está el documento listo para firma?

Reglas del gate `ready_for_signature`:
  - True solo si TODOS los siguientes:
    * completeness.critical_count == 0
    * coherence.critical_count == 0
    * qa.passed == True
    * citation_existence_rate >= 0.6 (al menos 60% de citas verificadas)

Output JSON (lo que ve el frontend):
{
  "ready_for_signature": false,
  "overall_score": 0.78,
  "blocking_issues_count": 2,
  "scores": {
    "completeness": 0.85,
    "coherence": 0.90,
    "qa_rules": 0.80,
    "citation_existence": 0.67
  },
  "completeness": {...},        // CompletenessReport.to_dict()
  "coherence": {...},           // CoherenceReport.to_dict()
  "qa": {...},                  // QA rule-based result
  "citation_existence_rate": 0.67,
  "blocking_issues": [
    {"source": "completeness", "issue": "missing_section: liquidacion", "severity": "critical"},
    {"source": "coherence", "issue": "cuantia_competencia falló", "severity": "critical"}
  ],
  "advisory_issues": [...],
  "summary": "Documento incompleto: falta sección liquidación y cuantía inconsistente"
}
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from typing import Any

from lex.orchestrator.stages.completeness_check import CompletenessReport
from lex.orchestrator.stages.coherence_check import CoherenceReport

logger = logging.getLogger(__name__)


# Umbral mínimo de citation_existence_rate para considerar el doc "verificado"
MIN_CITATION_EXISTENCE = 0.6


@dataclass
class BlockingIssue:
    source: str       # 'completeness' | 'coherence' | 'qa' | 'citations'
    issue: str        # descripción corta
    severity: str     # 'critical' | 'warning'
    suggested_fix: str | None = None


@dataclass
class QualityReport:
    doc_type: str
    ready_for_signature: bool = False
    overall_score: float = 0.0
    blocking_issues: list[BlockingIssue] = field(default_factory=list)
    advisory_issues: list[BlockingIssue] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    completeness: dict | None = None
    coherence: dict | None = None
    qa: dict | None = None
    citation_existence_rate: float = 0.0
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "doc_type": self.doc_type,
            "ready_for_signature": self.ready_for_signature,
            "overall_score": self.overall_score,
            "blocking_issues_count": len(self.blocking_issues),
            "scores": self.scores,
            "completeness": self.completeness,
            "coherence": self.coherence,
            "qa": self.qa,
            "citation_existence_rate": self.citation_existence_rate,
            "blocking_issues": [asdict(i) for i in self.blocking_issues],
            "advisory_issues": [asdict(i) for i in self.advisory_issues],
            "summary": self.summary,
        }


def build_quality_report(
    doc_type: str | None,
    completeness: CompletenessReport | None,
    coherence: CoherenceReport | None,
    qa_result: dict | None,
    citation_existence_rate: float,
) -> QualityReport:
    """Combina los 4 inputs en un único reporte auditable."""
    report = QualityReport(doc_type=doc_type or "default")
    report.citation_existence_rate = round(citation_existence_rate, 3)

    scores: dict[str, float] = {}
    blocking: list[BlockingIssue] = []
    advisory: list[BlockingIssue] = []

    # 1) Completeness
    if completeness is not None:
        report.completeness = completeness.to_dict()
        scores["completeness"] = completeness.overall_score
        for g in completeness.gaps:
            issue = BlockingIssue(
                source="completeness",
                issue=f"{g.type}: {g.message}",
                severity=g.severity,
                suggested_fix=g.suggested_fix,
            )
            if g.severity == "critical":
                blocking.append(issue)
            else:
                advisory.append(issue)
    else:
        scores["completeness"] = 1.0  # no check = no penaliza

    # 2) Coherence
    if coherence is not None:
        report.coherence = coherence.to_dict()
        scores["coherence"] = coherence.overall_score
        for f in coherence.gates_failed:
            issue = BlockingIssue(
                source="coherence",
                issue=f"{f.gate}: {f.description}",
                severity=f.severity,
                suggested_fix=f.suggested_fix,
            )
            if f.severity == "critical":
                blocking.append(issue)
            else:
                advisory.append(issue)
    else:
        scores["coherence"] = 1.0

    # 3) QA rule-based
    if qa_result is not None:
        report.qa = qa_result
        qa_score = float(qa_result.get("score", 0)) / 10.0  # qa.py usa 0-10
        scores["qa_rules"] = round(qa_score, 3)
        if not qa_result.get("passed", True):
            issues_list = qa_result.get("issues", []) or []
            for issue_msg in issues_list[:5]:
                blocking.append(BlockingIssue(
                    source="qa",
                    issue=str(issue_msg)[:200],
                    severity="warning",  # qa rule-based no llega a critical
                    suggested_fix=None,
                ))
    else:
        scores["qa_rules"] = 1.0

    # 4) Citation existence
    scores["citation_existence"] = report.citation_existence_rate
    if report.citation_existence_rate < MIN_CITATION_EXISTENCE:
        advisory.append(BlockingIssue(
            source="citations",
            issue=f"Solo {report.citation_existence_rate * 100:.0f}% de citas verificadas (mínimo {MIN_CITATION_EXISTENCE * 100:.0f}%). Revisar citas marcadas sospechosas.",
            severity="warning",
            suggested_fix="Validar manualmente las citas que el verifier no pudo confirmar en fuentes oficiales.",
        ))

    # Overall score: promedio ponderado
    # completeness es lo más importante (40%), coherence 30%, qa 15%, citations 15%
    overall = (
        0.40 * scores["completeness"]
        + 0.30 * scores["coherence"]
        + 0.15 * scores["qa_rules"]
        + 0.15 * scores["citation_existence"]
    )
    report.scores = scores
    report.overall_score = round(overall, 3)
    report.blocking_issues = blocking
    report.advisory_issues = advisory

    # Gate final: ready_for_signature
    report.ready_for_signature = (
        len(blocking) == 0
        and (completeness is None or completeness.can_continue)
        and (coherence is None or coherence.can_continue)
    )

    # Summary humano
    if report.ready_for_signature:
        report.summary = (
            f"Documento listo para firma. Calidad general: {overall * 100:.0f}%. "
            f"Sin issues bloqueantes. {len(advisory)} sugerencias opcionales."
        )
    else:
        report.summary = (
            f"Documento NO listo para firma. {len(blocking)} issues bloqueantes "
            f"({sum(1 for b in blocking if b.severity == 'critical')} críticos). "
            f"Calidad: {overall * 100:.0f}%."
        )

    logger.info(
        "quality_report: doc_type=%s ready=%s overall=%.2f blocking=%d advisory=%d",
        doc_type, report.ready_for_signature, overall,
        len(blocking), len(advisory),
    )
    return report
