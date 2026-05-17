"""Quality validator · checklist + vigencia + LLM-judge composite gate.

Used by:
  - agent/workers/document_generator.py (final VALIDATE stage)
  - api/multi_agent_generate.py (post-stream validation)
  - eval/templates_eval.py (Sprint 4 · scoring golden set)

Composes three layers:

  1. Programmatic checklist · YAML rules from `template_checklists`
     evaluated deterministically (presence, regex, structure).
  2. Vigencia checker · for each cited norm, query the existing
     `derogation.vigencia_checker` to confirm it's still in force.
  3. LLM-as-judge · `agent.llm_skills.judge.judge_quality` for the
     subjective rubric (formality, persuasiveness, faithfulness, etc.).

Returns a ValidationReport with a composite score and per-layer breakdown.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from agent.llm_skills import JudgeVerdict, judge_quality

logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    rule_id: str
    severity: str                       # 'blocking' | 'warning' | 'info'
    passed: bool
    detail: Optional[str] = None
    norm_reference: Optional[str] = None


@dataclass
class VigenciaWarning:
    norm_ref: str
    status: str                         # 'vigente' | 'derogada' | 'desconocida'
    detail: Optional[str] = None


@dataclass
class ValidationReport:
    checklist_score: float              # fraction of non-info rules passed
    checklist_results: list[CheckResult]
    vigencia_warnings: list[VigenciaWarning]
    judge: Optional[JudgeVerdict]
    composite_score: float              # weighted final score 0-1
    blocking: list[str] = field(default_factory=list)
    advisories: list[str] = field(default_factory=list)

    @property
    def has_blockers(self) -> bool:
        return bool(self.blocking)


async def validate_document(
    *,
    document_md: str,
    materia: str,
    doc_kind: str,
    jurisdiction: str = "CO",
    cited_norms: Optional[list[str]] = None,
    pool=None,
    judge_weights: Optional[dict[str, float]] = None,
    session_id: str = "",
) -> ValidationReport:
    """Run the three layers and combine into a single report."""

    # 1. Programmatic checklist.
    checklist_results = await _run_checklist(
        document_md=document_md,
        materia=materia,
        doc_kind=doc_kind,
        jurisdiction=jurisdiction,
        pool=pool,
    )

    # 2. Vigencia checker.
    vigencia_warnings = await _run_vigencia(
        cited_norms=cited_norms or _extract_norms_from_text(document_md),
        pool=pool,
    )

    # 3. LLM judge.
    judge_verdict: Optional[JudgeVerdict] = None
    try:
        r = await judge_quality(
            document_text=document_md,
            materia=materia,
            doc_kind=doc_kind,
            weights=judge_weights,
            purpose="validator:judge",
            session_id=session_id,
        )
        judge_verdict = r.data
    except Exception as e:
        logger.warning("validator judge failed (non-fatal): %s", e)

    # Aggregate.
    blocking: list[str] = []
    advisories: list[str] = []

    for c in checklist_results:
        if not c.passed and c.severity == "blocking":
            blocking.append(f"{c.rule_id}: {c.detail or 'falló'}")
        elif not c.passed and c.severity == "warning":
            advisories.append(f"{c.rule_id}: {c.detail or 'advertencia'}")

    for vw in vigencia_warnings:
        if vw.status == "derogada":
            blocking.append(f"Norma derogada citada: {vw.norm_ref}")
        elif vw.status == "desconocida":
            advisories.append(f"Vigencia no verificada: {vw.norm_ref}")

    if judge_verdict:
        blocking.extend(judge_verdict.critical_issues)
        advisories.extend(judge_verdict.warnings)

    # Scoring.
    relevant = [c for c in checklist_results if c.severity != "info"]
    if relevant:
        checklist_score = sum(1 for c in relevant if c.passed) / len(relevant)
    else:
        checklist_score = 1.0

    judge_score = judge_verdict.overall_score if judge_verdict else 0.0
    # Composite: 60% judge + 40% checklist · subtract vigencia warnings.
    composite = 0.6 * judge_score + 0.4 * checklist_score
    composite -= 0.05 * sum(1 for v in vigencia_warnings if v.status == "derogada")
    composite = max(0.0, min(1.0, composite))

    return ValidationReport(
        checklist_score=round(checklist_score, 3),
        checklist_results=checklist_results,
        vigencia_warnings=vigencia_warnings,
        judge=judge_verdict,
        composite_score=round(composite, 3),
        blocking=blocking,
        advisories=advisories,
    )


# ──────────────────────────────────────────────────────────────
# Checklist layer
# ──────────────────────────────────────────────────────────────


async def _run_checklist(
    *,
    document_md: str,
    materia: str,
    doc_kind: str,
    jurisdiction: str,
    pool,
) -> list[CheckResult]:
    """Load YAML rules from `template_checklists` and evaluate them."""
    if not pool:
        # No DB available · run only built-in defaults so callers still get
        # a baseline signal (useful in unit tests).
        return _builtin_rules(document_md, doc_kind)

    rules_yaml: Optional[str] = None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                select checklist_yaml
                  from template_checklists
                 where materia = $1::materia_legal
                   and doc_type = $2
                   and jurisdiction = $3
                   and is_active = true
                 order by version desc
                 limit 1
                """,
                materia, doc_kind, jurisdiction,
            )
            if row:
                rules_yaml = row["checklist_yaml"]
    except Exception as e:
        logger.warning("template_checklists query failed: %s", e)

    rules: list[dict] = []
    if rules_yaml:
        rules = _parse_yaml_rules(rules_yaml)

    if not rules:
        return _builtin_rules(document_md, doc_kind)

    results: list[CheckResult] = []
    for rule in rules:
        results.append(_evaluate_rule(rule, document_md))
    # Always append baseline rules even when DB rules exist · they're cheap.
    results.extend(_builtin_rules(document_md, doc_kind))
    return results


def _parse_yaml_rules(yaml_text: str) -> list[dict]:
    """Parse the YAML rules · fall back to empty list on any error.

    Schema expected:
      rules:
        - id: parties_completos
          severity: blocking
          description: ...
          pattern: "^I\\."          # optional regex on document_md
          contains_any: ["Pretensiones"]
          contains_all: ["Hechos", "Pretensiones"]
          min_length: 500
    """
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not installed · skipping DB checklist rules")
        return []
    try:
        data = yaml.safe_load(yaml_text) or {}
    except Exception as e:
        logger.warning("yaml parse failed: %s", e)
        return []
    rules = data.get("rules") if isinstance(data, dict) else None
    return rules if isinstance(rules, list) else []


def _evaluate_rule(rule: dict, document_md: str) -> CheckResult:
    rid = rule.get("id", "unknown")
    severity = rule.get("severity", "warning")
    detail = rule.get("description")
    passed = True

    if "min_length" in rule:
        if len(document_md) < int(rule["min_length"]):
            passed = False
            detail = f"longitud < {rule['min_length']}"

    if passed and "contains_all" in rule:
        for needle in rule.get("contains_all", []):
            if needle.lower() not in document_md.lower():
                passed = False
                detail = f"falta '{needle}'"
                break

    if passed and "contains_any" in rule:
        any_match = any(n.lower() in document_md.lower() for n in rule.get("contains_any", []))
        if not any_match:
            passed = False
            detail = f"ninguna de: {', '.join(rule.get('contains_any', []))}"

    if passed and "pattern" in rule:
        try:
            if not re.search(rule["pattern"], document_md, flags=re.MULTILINE):
                passed = False
                detail = f"pattern no encontrado: {rule['pattern']}"
        except re.error as e:
            passed = False
            detail = f"regex inválida: {e}"

    return CheckResult(
        rule_id=rid,
        severity=severity,
        passed=passed,
        detail=detail,
        norm_reference=rule.get("norm_reference"),
    )


def _builtin_rules(document_md: str, doc_kind: str) -> list[CheckResult]:
    """Defaults applied to every document · cheap to evaluate."""
    results: list[CheckResult] = []
    text = document_md.lower()

    results.append(CheckResult(
        rule_id="min_length",
        severity="blocking",
        passed=len(document_md.strip()) > 300,
        detail=None if len(document_md.strip()) > 300 else "documento sospechosamente corto",
    ))

    results.append(CheckResult(
        rule_id="has_notificaciones",
        severity="warning",
        passed=("notificacion" in text or "notificación" in text),
        detail=None if ("notificacion" in text or "notificación" in text)
               else "no se detectó sección de notificaciones",
    ))

    results.append(CheckResult(
        rule_id="has_firma_placeholder",
        severity="info",
        passed=("firma" in text or "atentamente" in text),
        detail=None,
    ))

    if doc_kind == "tutela":
        results.append(CheckResult(
            rule_id="tutela_jurado",
            severity="blocking",
            passed=("juramento" in text or "bajo gravedad de juramento" in text),
            detail=None if "juramento" in text else "falta el juramento del art. 38 Dec. 2591/91",
        ))
        results.append(CheckResult(
            rule_id="tutela_derecho_fundamental",
            severity="blocking",
            passed=("derecho fundamental" in text or "derechos fundamentales" in text),
            detail=None if "derecho fundamental" in text else "no se invocan derechos fundamentales",
        ))

    if doc_kind in ("demanda", "demanda_civil", "demanda_laboral"):
        results.append(CheckResult(
            rule_id="demanda_cuantia",
            severity="blocking",
            passed=("cuantia" in text or "cuantía" in text),
            detail=None if "cuantía" in text else "falta cuantía (Art. 25 CGP)",
        ))
        results.append(CheckResult(
            rule_id="demanda_pretensiones",
            severity="blocking",
            passed=("pretensiones" in text or "peticiones" in text),
            detail=None if "pretensiones" in text else "falta sección de pretensiones",
        ))

    return results


# ──────────────────────────────────────────────────────────────
# Vigencia layer
# ──────────────────────────────────────────────────────────────


_NORM_RE = re.compile(
    r"(?:Ley|Decreto|Resoluci[oó]n|Acuerdo)\s+\d+(?:\s*(?:de|/)\s*\d{2,4})?"
    r"|(?:CST|CGP|CPACA|CC|C\.P\.)[\s\.]+art(?:[íi]culo|\.)\s*\d+",
    flags=re.IGNORECASE,
)


def _extract_norms_from_text(text: str) -> list[str]:
    """Cheap regex pass · returns unique, deduplicated normative refs."""
    if not text:
        return []
    matches = _NORM_RE.findall(text)
    seen: set[str] = set()
    out: list[str] = []
    for m in matches:
        norm = " ".join(m.split())
        if norm.lower() not in seen:
            seen.add(norm.lower())
            out.append(norm)
    return out[:30]


async def _run_vigencia(
    *,
    cited_norms: list[str],
    pool,
) -> list[VigenciaWarning]:
    """Query the existing derogation/vigencia_checker for each cited norm.

    If the module isn't importable or fails, we mark all norms as 'desconocida'
    (advisory, non-blocking).
    """
    if not cited_norms:
        return []

    try:
        from derogation.vigencia_checker import VigenciaChecker
    except Exception:
        return [
            VigenciaWarning(norm_ref=n, status="desconocida",
                            detail="vigencia_checker no disponible")
            for n in cited_norms
        ]

    warnings: list[VigenciaWarning] = []
    checker = VigenciaChecker(pool) if pool else None
    for norm in cited_norms:
        try:
            if checker is None:
                warnings.append(VigenciaWarning(
                    norm_ref=norm, status="desconocida",
                    detail="sin pool de BD",
                ))
                continue
            res = await checker.check(norm)  # contrato esperado · best-effort
            status = "vigente"
            detail = None
            if isinstance(res, dict):
                if res.get("derogada") or res.get("vigente") is False:
                    status = "derogada"
                    detail = res.get("razon") or res.get("derogada_por")
            warnings.append(VigenciaWarning(norm_ref=norm, status=status, detail=detail))
        except Exception as e:
            warnings.append(VigenciaWarning(
                norm_ref=norm, status="desconocida", detail=str(e)[:120],
            ))

    return warnings
