"""M19.20.B — Stage Completeness Check.

Valida que un documento generado cumpla con su TemplateContract
(lex/templates/contracts.py) antes de marcarlo como "ready_for_signature".

Dos capas de validación:
  1. RULE-BASED (rápida, determinística):
     - ¿Están todas las secciones obligatorias?
     - ¿Cada sección tiene el mínimo de bloques esperados?
     - ¿Las secciones que NO aplican (ej. juramento en tutela) están ausentes?
     - ¿Hay bloques `juramento` con norma_ref correcta para el doc_type?
     - ¿Las pretensiones usan verbos válidos para el área?

  2. LLM-BASED (más cara, valida contenido sustantivo):
     - ¿Hay secciones truncadas (1-2 bloques sustantivos donde debería haber 5+)?
     - ¿El contenido de cada sección es coherente con su tema?
     - ¿Las notas_calidad del contrato se cumplen?

Output: `CompletenessReport` con:
  - gaps: lista de problemas
  - critical_count: cuántos son blockers
  - can_continue: True si no hay críticos
  - rule_score: 0.0–1.0 (porcentaje de rules pasadas)
  - llm_score: 0.0–1.0 (cuando aplica)
  - overall_score: weighted average

Si `can_continue=False`, el orchestrator debería invocar M19.20.E (auto-loop)
para regenerar las secciones gap.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from typing import Any, Literal

from lex.templates.contracts import TemplateContract, get_contract

logger = logging.getLogger(__name__)


GapSeverity = Literal["critical", "warning", "info"]


@dataclass
class Gap:
    """Un gap individual de completitud."""
    type: str                       # 'missing_section' | 'insufficient_blocks' | 'wrong_norma' | 'truncated' | 'missing_subsection' | 'wrong_verb' | etc.
    section_key: str | None
    severity: GapSeverity
    message: str
    suggested_fix: str | None = None  # acción concreta para resolver
    block_id: str | None = None        # bloque afectado (cuando aplica)


@dataclass
class CompletenessReport:
    doc_type: str
    contract_descripcion: str
    gaps: list[Gap] = field(default_factory=list)
    rule_score: float = 1.0        # porcentaje de reglas declarativas pasadas
    llm_score: float | None = None  # porcentaje del LLM check (None si no se corrió)
    overall_score: float = 1.0
    can_continue: bool = True       # False si critical_count > 0

    @property
    def critical_count(self) -> int:
        return sum(1 for g in self.gaps if g.severity == "critical")

    @property
    def warning_count(self) -> int:
        return sum(1 for g in self.gaps if g.severity == "warning")

    def to_dict(self) -> dict:
        return {
            "doc_type": self.doc_type,
            "contract_descripcion": self.contract_descripcion,
            "rule_score": self.rule_score,
            "llm_score": self.llm_score,
            "overall_score": self.overall_score,
            "can_continue": self.can_continue,
            "critical_count": self.critical_count,
            "warning_count": self.warning_count,
            "gaps": [asdict(g) for g in self.gaps],
        }


# ============================================================
# Helpers de extracción sobre lista de bloques
# ============================================================

def _block_type(b: dict) -> str:
    return b.get("block_type") or b.get("block_data", {}).get("type") or "?"


def _section_key(b: dict) -> str:
    return b.get("section_key") or b.get("block_data", {}).get("section_key") or ""


def _block_data(b: dict) -> dict:
    return b.get("block_data") or {}


def _block_text(b: dict) -> str:
    bd = _block_data(b)
    runs = bd.get("runs") or []
    if isinstance(runs, list):
        return "".join(
            r.get("text", "") if isinstance(r, dict) else str(r)
            for r in runs
        )
    return bd.get("text", "") or ""


def _present_sections(blocks: list[dict]) -> set[str]:
    """Sections con al menos 1 bloque (incluyendo section_heading)."""
    out: set[str] = set()
    for b in blocks:
        sk = _section_key(b)
        if sk:
            out.add(sk)
        # Heurística: si es section_heading, su section_key declara la sección
        if _block_type(b) == "section_heading":
            sk2 = _block_data(b).get("section_key", "")
            if sk2:
                out.add(sk2)
    return out


def _blocks_in_section(blocks: list[dict], section_key: str, only_type: str | None = None) -> list[dict]:
    """Bloques de una sección específica, opcionalmente filtrados por tipo."""
    out = []
    for b in blocks:
        if _section_key(b) != section_key:
            continue
        if only_type and _block_type(b) != only_type:
            continue
        out.append(b)
    return out


def _juramento_blocks(blocks: list[dict]) -> list[dict]:
    return [b for b in blocks if _block_type(b) == "juramento"]


def _firma_blocks(blocks: list[dict]) -> list[dict]:
    return [b for b in blocks if _block_type(b) == "firma"]


# ============================================================
# Capa 1 — Rule-based check
# ============================================================

def _check_rules(blocks: list[dict], contract: TemplateContract) -> list[Gap]:
    """Validaciones declarativas sin LLM. Rápidas y deterministas."""
    gaps: list[Gap] = []
    present = _present_sections(blocks)

    # 1. Secciones obligatorias presentes
    for sk in contract.get("secciones_obligatorias", []):
        if sk not in present:
            gaps.append(Gap(
                type="missing_section",
                section_key=sk,
                severity="critical",
                message=f"Falta sección obligatoria '{sk}' para doc_type '{contract.get('doc_type')}'.",
                suggested_fix=f"Generar sección '{sk}' con su contenido propio.",
            ))

    # 2. Secciones que NO deben aparecer
    for sk in contract.get("secciones_no_aplican", []):
        if sk in present:
            gaps.append(Gap(
                type="forbidden_section",
                section_key=sk,
                severity="warning",
                message=f"Sección '{sk}' NO aplica a doc_type '{contract.get('doc_type')}' (ej. juramento en tutela).",
                suggested_fix=f"Eliminar bloques de la sección '{sk}'.",
            ))

    # 3. Mínimo de bloques por sección
    for sk, rule in contract.get("min_bloques_por_seccion", {}).items():
        actual_blocks = _blocks_in_section(blocks, sk, only_type=rule.get("type"))
        # Excluir headings y blanks del conteo
        substantive = [b for b in actual_blocks if _block_type(b) not in ("section_heading", "blank", "subsection")]
        if len(substantive) < rule["min"]:
            type_hint = f" tipo '{rule.get('type')}'" if rule.get("type") else ""
            gaps.append(Gap(
                type="insufficient_blocks",
                section_key=sk,
                severity="warning" if len(substantive) >= 1 else "critical",
                message=f"Sección '{sk}' tiene {len(substantive)} bloques{type_hint} sustantivos; se esperan al menos {rule['min']}.",
                suggested_fix=f"Generar {rule['min'] - len(substantive)} bloques adicionales en '{sk}'.",
            ))
        # blocks_required (al menos uno de cada tipo)
        for required_type in rule.get("blocks_required", []):
            if not any(_block_type(b) == required_type for b in actual_blocks):
                gaps.append(Gap(
                    type="missing_block_type",
                    section_key=sk,
                    severity="warning",
                    message=f"Sección '{sk}' no contiene ningún bloque tipo '{required_type}'.",
                    suggested_fix=f"Agregar al menos un bloque '{required_type}' a '{sk}'.",
                ))

    # 4. Juramento con norma_ref correcta
    if contract.get("requires_juramento", False):
        jur = _juramento_blocks(blocks)
        if not jur:
            gaps.append(Gap(
                type="missing_juramento",
                section_key="juramento",
                severity="critical",
                message="No hay bloque de juramento y el doc_type lo requiere.",
                suggested_fix=f"Agregar bloque juramento con norma_ref='{contract.get('juramento_norma_ref', '')}'.",
            ))
        else:
            expected_norma = (contract.get("juramento_norma_ref") or "").lower().strip()
            for jb in jur:
                actual_norma = (_block_data(jb).get("norma_ref") or "").lower().strip()
                if expected_norma and actual_norma != expected_norma:
                    gaps.append(Gap(
                        type="wrong_juramento_norma",
                        section_key="juramento",
                        severity="critical",
                        message=f"Juramento usa norma '{_block_data(jb).get('norma_ref')}' pero el doc_type '{contract.get('doc_type')}' requiere '{contract.get('juramento_norma_ref')}'.",
                        suggested_fix=f"Cambiar norma_ref del juramento a '{contract.get('juramento_norma_ref')}'.",
                        block_id=jb.get("block_id"),
                    ))
    else:
        # No requiere juramento (ej. tutela). Si hay, es gap.
        for jb in _juramento_blocks(blocks):
            gaps.append(Gap(
                type="forbidden_juramento",
                section_key=_section_key(jb) or "juramento",
                severity="warning",
                message=f"El doc_type '{contract.get('doc_type')}' NO requiere juramento separado.",
                suggested_fix="Eliminar bloque juramento.",
                block_id=jb.get("block_id"),
            ))

    # 5. Firma presente si requires_firma
    if contract.get("requires_firma", True):
        firma = _firma_blocks(blocks)
        if not firma:
            gaps.append(Gap(
                type="missing_firma",
                section_key="firma",
                severity="critical",
                message="No hay bloque de firma y el doc_type lo requiere.",
                suggested_fix="Agregar bloque firma con datos del apoderado.",
            ))
        elif len(firma) > 1:
            gaps.append(Gap(
                type="duplicate_firma",
                section_key="firma",
                severity="warning",
                message=f"Hay {len(firma)} bloques de firma; debe haber solo 1.",
                suggested_fix="Eliminar bloques de firma duplicados.",
            ))

    # 6. Pretensiones con verbos válidos
    valid_verbs = [v.upper() for v in contract.get("pretensiones_verbos_validos", [])]
    if valid_verbs:
        for b in blocks:
            if _block_type(b) != "pretension":
                continue
            txt = _block_text(b).strip().upper()
            if not any(txt.startswith(v) for v in valid_verbs):
                gaps.append(Gap(
                    type="wrong_pretension_verb",
                    section_key=_section_key(b),
                    severity="info",
                    message=f"Pretensión inicia con verbo no estándar; esperados: {', '.join(valid_verbs)}.",
                    suggested_fix=f"Reformular la pretensión iniciando con uno de: {', '.join(valid_verbs)}.",
                    block_id=b.get("block_id"),
                ))

    # 7. Cuerpos normativos mínimos invocados
    cuerpos_min = contract.get("cuerpos_normativos_minimos", [])
    if cuerpos_min:
        all_text = " ".join(_block_text(b) for b in blocks) + " ".join(
            _block_data(b).get("norma", "") for b in blocks if _block_type(b) == "norma_citada"
        )
        upper_text = all_text.upper()
        for cuerpo in cuerpos_min:
            if cuerpo.upper() not in upper_text:
                gaps.append(Gap(
                    type="missing_cuerpo_normativo",
                    section_key="fundamentos",
                    severity="warning",
                    message=f"El documento no invoca ninguna norma del cuerpo '{cuerpo}' (esperado para doc_type '{contract.get('doc_type')}').",
                    suggested_fix=f"Citar al menos un artículo de '{cuerpo}' en los fundamentos de derecho.",
                ))

    # 8. Liquidación si aplica
    if contract.get("requires_liquidacion", False):
        liq_types = [_block_type(b) for b in blocks]
        has_calc = "calc_step" in liq_types or "table" in liq_types
        if not has_calc:
            gaps.append(Gap(
                type="missing_liquidacion",
                section_key="liquidacion",
                severity="critical",
                message="El doc_type requiere liquidación pero no hay bloques calc_step ni table.",
                suggested_fix="Generar tabla de liquidación con conceptos cuantificados.",
            ))

    return gaps


# ============================================================
# Capa 2 — LLM substantive check (opcional)
# ============================================================

LLM_SUBSTANTIVE_PROMPT = """Eres un ABOGADO LITIGANTE SENIOR colombiano revisando un memorial antes de firma.
Tu único trabajo en esta tarea es detectar dos cosas en el documento:

  A) **TRUNCAMIENTOS**: secciones que terminan abruptamente, párrafos cortados,
     enumeraciones incompletas, o contenido que claramente no terminó.
  B) **VACÍOS SUSTANTIVOS**: secciones presentes pero sin contenido real
     (placeholders sin completar, frases genéricas que no aportan, etc.).

NO te preocupes por:
  - Calidad estilística (otro stage lo hace).
  - Coherencia cross-section (otro stage lo hace).
  - Citas verificadas (otro stage lo hace).

Te paso:
  1. Doc type y descripción del contrato.
  2. Lista de secciones presentes con conteo de bloques.
  3. Texto resumido de cada sección (primeros 600 chars).
  4. Notas de calidad específicas del tipo de documento.

Devuelve JSON:
{
  "truncations": [{"section_key": "...", "evidence": "frase final que parece cortada"}],
  "empty_substantive": [{"section_key": "...", "issue": "qué falta sustancialmente"}],
  "quality_score": 0.0,   // 0=desastroso, 1=excelente
  "notas_check": [{"nota_idx": 0, "passes": true|false, "comment": "..."}]
}
NO inventes problemas. Si todo está completo, devuelve listas vacías y score >= 0.9.
"""


async def _llm_substantive_check(
    client,
    blocks: list[dict],
    contract: TemplateContract,
) -> tuple[float, list[Gap]]:
    """Corre el LLM substantive check. Retorna (llm_score, llm_gaps).

    Si falla, retorna (1.0, []) — no debe romper el orchestrator.
    """
    try:
        # Construir resumen
        sections_summary = {}
        for b in blocks:
            sk = _section_key(b)
            if not sk:
                continue
            sections_summary.setdefault(sk, []).append(b)

        sec_lines = []
        for sk, sb in sections_summary.items():
            text = " ".join(_block_text(b) for b in sb)[:600]
            sec_lines.append(f"  [{sk}] ({len(sb)} bloques): {text!r}")

        notas = contract.get("notas_calidad", [])
        notas_lines = [f"  [{i}] {n}" for i, n in enumerate(notas)]

        user_prompt = f"""DOC TYPE: {contract.get('doc_type')}
DESCRIPCIÓN: {contract.get('descripcion')}

SECCIONES OBLIGATORIAS DECLARADAS:
{', '.join(contract.get('secciones_obligatorias', []))}

SECCIONES PRESENTES (con conteo y muestra de texto):
{chr(10).join(sec_lines) if sec_lines else '  (ninguna)'}

NOTAS DE CALIDAD ESPECÍFICAS:
{chr(10).join(notas_lines) if notas_lines else '  (ninguna)'}

Revisa truncamientos y vacíos sustantivos. Devuelve JSON."""

        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": LLM_SUBSTANTIVE_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=1500,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        llm_gaps: list[Gap] = []
        for t in data.get("truncations", []) or []:
            if not isinstance(t, dict):
                continue
            llm_gaps.append(Gap(
                type="llm_truncation",
                section_key=str(t.get("section_key", ""))[:40],
                severity="critical",
                message=f"Sección truncada: {str(t.get('evidence', ''))[:200]}",
                suggested_fix="Completar el contenido de la sección.",
            ))
        for v in data.get("empty_substantive", []) or []:
            if not isinstance(v, dict):
                continue
            llm_gaps.append(Gap(
                type="llm_empty_substantive",
                section_key=str(v.get("section_key", ""))[:40],
                severity="warning",
                message=f"Vacío sustantivo: {str(v.get('issue', ''))[:200]}",
                suggested_fix="Redactar contenido real (no placeholders genéricos).",
            ))
        # Notas que no pasan
        for n in data.get("notas_check", []) or []:
            if not isinstance(n, dict) or n.get("passes"):
                continue
            idx = n.get("nota_idx")
            nota_text = notas[idx] if isinstance(idx, int) and 0 <= idx < len(notas) else "?"
            llm_gaps.append(Gap(
                type="llm_quality_nota",
                section_key=None,
                severity="info",
                message=f"Nota de calidad incumplida: {nota_text}",
                suggested_fix=str(n.get("comment", ""))[:200] or "Revisar nota de calidad del template.",
            ))
        score = data.get("quality_score", 1.0)
        try:
            score = float(score)
            score = max(0.0, min(1.0, score))
        except Exception:
            score = 1.0
        return score, llm_gaps
    except Exception as e:
        logger.warning("llm_substantive_check failed (non-fatal): %s", e)
        return 1.0, []


# ============================================================
# Orchestration helper
# ============================================================

def _derive_contract_from_recipe(structure_recipe: dict | None) -> TemplateContract | None:
    """M19.24.D.5 — Deriva un TemplateContract dinámicamente del structure_recipe.

    Si el recipe tiene sections_plan y los campos requires_*, construye un
    contrato sin depender de TEMPLATE_CONTRACTS hardcoded. Esto permite
    validar documentos no-demanda (poderes, contratos, etc.) correctamente.

    Returns None si el recipe no tiene info suficiente.
    """
    if not structure_recipe:
        return None
    sections_plan = structure_recipe.get("sections_plan") or []
    if not isinstance(sections_plan, list) or len(sections_plan) == 0:
        return None

    secciones_obligatorias = [
        s.get("key", "") for s in sections_plan if isinstance(s, dict) and s.get("key")
    ]
    if not secciones_obligatorias:
        return None

    # secciones_no_aplican: si NO requiere pretensiones, hechos o juramento,
    # las marcamos como no aplican
    no_aplican = []
    req_pret = structure_recipe.get("requires_pretensiones")
    req_hechos = structure_recipe.get("requires_hechos")
    req_juramento = structure_recipe.get("requires_juramento")
    if req_pret is False:
        no_aplican.append("pretensiones")
    if req_hechos is False:
        no_aplican.append("hechos")
    if req_juramento is False:
        no_aplican.append("juramento")
        no_aplican.append("competencia_cuantia")

    # Construir TemplateContract dict-like
    contract: dict = {
        "doc_type": structure_recipe.get("doc_type", "default"),
        "area": structure_recipe.get("jurisdiccion", "general"),
        "descripcion": f"Recipe M19.24 ({structure_recipe.get('document_family', '?')})",
        "secciones_obligatorias": secciones_obligatorias,
        "secciones_no_aplican": no_aplican,
        "min_bloques_por_seccion": {},
        "juramento_norma_ref": structure_recipe.get("juramento_norma_ref", "") or "",
        "competencia_juez_default": structure_recipe.get("juez_competente", "") or "",
        "cuerpos_normativos_minimos": structure_recipe.get("cuerpos_normativos_minimos", []) or [],
        "pretensiones_verbos_validos": ["DECLARAR", "CONDENAR", "ORDENAR", "SOLICITAR"],
        "requires_liquidacion": False,
        "requires_juramento": bool(req_juramento) if req_juramento is not None else False,
        "requires_firma": True,
        "notas_calidad": [],
    }
    return contract  # type: ignore[return-value]


async def check_completeness(
    client,
    blocks: list[dict],
    doc_type: str | None,
    run_llm_check: bool = True,
    structure_recipe: dict | None = None,
) -> CompletenessReport:
    """Punto de entrada principal. El orchestrator lo invoca post-block_gen.

    M19.24.D.5: si structure_recipe está presente, deriva el contrato
    dinámicamente. Si no, fallback a get_contract() hardcoded.
    """
    contract = _derive_contract_from_recipe(structure_recipe) or get_contract(doc_type)
    rule_gaps = _check_rules(blocks, contract)
    # Score rule-based: % de validaciones que pasaron
    # Estimación: 5 categorías × 2 puntos cada una = 10. Cada gap critical resta 2, warning resta 1, info resta 0.5
    penalty = sum(
        2.0 if g.severity == "critical" else (1.0 if g.severity == "warning" else 0.5)
        for g in rule_gaps
    )
    rule_score = max(0.0, min(1.0, 1.0 - penalty / 10.0))

    llm_score = None
    llm_gaps: list[Gap] = []
    if run_llm_check and client is not None:
        llm_score, llm_gaps = await _llm_substantive_check(client, blocks, contract)

    all_gaps = rule_gaps + llm_gaps
    if llm_score is not None:
        overall = 0.6 * rule_score + 0.4 * llm_score
    else:
        overall = rule_score

    critical_count = sum(1 for g in all_gaps if g.severity == "critical")
    report = CompletenessReport(
        doc_type=doc_type or "default",
        contract_descripcion=contract.get("descripcion", ""),
        gaps=all_gaps,
        rule_score=round(rule_score, 3),
        llm_score=round(llm_score, 3) if llm_score is not None else None,
        overall_score=round(overall, 3),
        can_continue=(critical_count == 0),
    )
    logger.info(
        "completeness_check: doc_type=%s rule_score=%.2f llm_score=%s critical=%d warning=%d can_continue=%s",
        doc_type, rule_score, f"{llm_score:.2f}" if llm_score is not None else "n/a",
        critical_count, report.warning_count, report.can_continue,
    )
    return report
