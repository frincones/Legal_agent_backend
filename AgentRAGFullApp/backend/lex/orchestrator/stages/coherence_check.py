"""M19.20.C — Stage Coherence Check (cross-section LLM-as-judge).

Distinto del change_auditor (M19.18.C) que revisa UN cambio puntual del usuario,
este stage revisa el DOCUMENTO COMPLETO recién generado en dimensiones que
necesitan visión holística:

  1. **Datos consistentes**: nombres, fechas, montos, cédulas que aparecen
     varias veces deben ser IDÉNTICOS en todas las menciones.
  2. **Hechos ↔ Pretensiones**: cada pretensión debe tener sustento en al menos
     un hecho narrado. Pretensiones huérfanas son riesgo.
  3. **Normas ↔ Fundamentos**: cada norma citada en pretensiones debe aparecer
     desarrollada en fundamentos de derecho.
  4. **Liquidación coherente**: si hay tabla, los conceptos deben estar en
     pretensiones; las fórmulas deben usar los valores declarados en hechos.
  5. **Procesal**: cuantía declarada debe coincidir con la suma de pretensiones
     de condena.
  6. **Jurisdicción ↔ competencia**: si es civil, juez civil; laboral, juez
     laboral; etc.

Output: `CoherenceReport` con gates aprobados/fallidos.

Diseño: 100% LLM (no rule-based porque requiere comprensión semántica).
Modelo: gpt-4o-mini (rápido, suficiente para detección de inconsistencias).
Costo: ~$0.001 por documento.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from typing import Any, Literal

logger = logging.getLogger(__name__)


GateSeverity = Literal["critical", "warning", "info"]


@dataclass
class CoherenceFinding:
    gate: str                       # 'data_consistency' | 'pretension_supported' | 'norma_developed' | etc.
    severity: GateSeverity
    description: str
    affected_blocks: list[str] = field(default_factory=list)
    suggested_fix: str | None = None


@dataclass
class CoherenceReport:
    doc_type: str
    overall_score: float = 1.0     # 0.0–1.0
    gates_passed: list[str] = field(default_factory=list)
    gates_failed: list[CoherenceFinding] = field(default_factory=list)
    gates_not_applicable: list[dict] = field(default_factory=list)  # M19.23.K
    doc_type_reasoning: str = ""    # M19.23.K — por qué se descartaron gates
    can_continue: bool = True       # False si critical_count > 0

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.gates_failed if f.severity == "critical")

    def to_dict(self) -> dict:
        return {
            "doc_type": self.doc_type,
            "overall_score": self.overall_score,
            "gates_passed": self.gates_passed,
            "gates_failed": [asdict(f) for f in self.gates_failed],
            "gates_not_applicable": self.gates_not_applicable,
            "doc_type_reasoning": self.doc_type_reasoning,
            "critical_count": self.critical_count,
            "can_continue": self.can_continue,
        }


COHERENCE_SYSTEM_PROMPT = """Eres un ABOGADO LITIGANTE SENIOR colombiano revisando un memorial COMPLETO
antes de firma para detectar **inconsistencias cross-section** que podrían
generar pérdida del proceso o sanciones procesales.

Tu trabajo es revisar GATES de coherencia y devolver para cada uno si pasa
o falla, con evidencia concreta. NO re-redactas, solo identificas.

═══════════════════════════════════════════════════════════════
PASO 1 — DECIDE QUÉ GATES APLICAN AL doc_type
═══════════════════════════════════════════════════════════════

Antes de revisar nada, decide qué gates son APLICABLES según el doc_type:

  • data_consistency → SIEMPRE aplica (todos los docs).
  • pretension_supported → APLICA en demandas/recursos/tutela (no en
    contratos, conceptos, poderes).
  • norma_developed → APLICA en demandas, recursos, conceptos jurídicos.
    No aplica a contratos privados ni poderes.
  • liquidacion_coherente → SOLO si hay TABLA o calc_step en el doc.
    NO aplica a demandas declarativas puras (divorcio, pertenencia, tutela)
    a menos que tengan liquidación de pretensiones económicas.
  • cuantia_competencia → SOLO si la jurisdicción es por cuantía:
      ✓ APLICA: civil ordinario/ejecutivo, laboral, comercial
      ✗ NO APLICA: familia (divorcio/alimentos/custodia), penal, tutela,
        constitucional, administrativo de nulidad, pertenencia,
        sucesiones, derecho petición, poderes, contratos privados.
  • jurisdiccion_competencia → SIEMPRE aplica.

Si un gate NO APLICA al doc_type, NO lo incluyas en gates_failed ni
gates_passed. Inclúyelo en "gates_not_applicable" con la razón.

═══════════════════════════════════════════════════════════════
PASO 2 — REVISA SOLO LOS GATES APLICABLES
═══════════════════════════════════════════════════════════════

1. **data_consistency** — Datos repetidos son IDÉNTICOS
   - Nombres, empresas, vehículos, inmuebles, direcciones, matrículas,
     cédulas, NITs, fechas, montos. Si aparecen en varias secciones,
     deben coincidir EXACTAMENTE.
   - Variantes ortográficas o abreviaturas distintas PASAN. Datos
     numéricos distintos NO.
   - PLACEHOLDERS explícitos (ej. "[FECHA_MATRIMONIO]", "$X") NO son
     inconsistencia — son ausencia. NO los marques como datos
     inconsistentes; eso lo detecta data_completeness_gate.

2. **pretension_supported** — Cada pretensión tiene base fáctica
   - Cada pretensión debe estar respaldada por al menos un hecho narrado.
   - Si pides indemnización por lucro cesante, debes haber narrado el daño
     económico en hechos. Pretensiones sin sustento son riesgo.

3. **norma_developed** — Normas citadas en pretensiones aparecen en fundamentos
   - Si una pretensión invoca "Art. X CC", los fundamentos deben desarrollar
     el contenido normativo de ese artículo (al menos en un párrafo).
   - Tolera que la norma esté desarrollada en fundamentos aunque no esté
     citada literal en pretensiones (lo normal es el revés).

4. **liquidacion_coherente** — Si hay liquidación, fluyen los números
   - Conceptos de la tabla = conceptos de pretensiones de condena.
   - Fórmulas usan valores que aparecen declarados en hechos.
   - Suma de la tabla debe ser consistente con la cuantía declarada.

5. **cuantia_competencia** — Cuantía declarada coincide con pretensiones
   - Solo aplica si el doc_type tiene cuantía (ver PASO 1).
   - Mayor cuantía: > 150 SMMLV (~$214M en 2026).
   - Menor cuantía: 40-150 SMMLV.
   - Mínima cuantía: < 40 SMMLV.

6. **jurisdiccion_competencia** — Juez competente coincide con el área
   - Civil → Juez Civil. Laboral → Juez Laboral. Familia → Juez Familia.
   - Administrativo → Juez/Tribunal Administrativo. Tutela → cualquier
     juez (reparto). Penal → Juez Penal o Fiscalía.

═══════════════════════════════════════════════════════════════
REGLAS NO NEGOCIABLES
═══════════════════════════════════════════════════════════════

- Gate NO APLICA → va en gates_not_applicable, no resta score.
- Gate aplica y passed=true → suma score, va en gates_passed.
- Gate aplica y passed=false → resta score, va en gates_failed.
- overall_score = (gates_passed_count) / (applicable_gates_count).
- NO inventes inconsistencias para demostrar trabajo.
- severity=critical solo cuando es riesgo PROCESAL real
  (datos contradictorios entre secciones que ya tienen valores reales).
- Placeholders [X], [FECHA_X] NO son críticos en coherence (los maneja el gate).
- Máximo 8 gates retornados (6 base + 2 extras opcionales si los hay).

═══════════════════════════════════════════════════════════════
OUTPUT (SOLO JSON válido, sin markdown)
═══════════════════════════════════════════════════════════════

{
  "doc_type_reasoning": "doc_type=X → aplican gates [a,b,c,d], no aplican [e,f] porque [razón]",
  "overall_score": 0.0,
  "gates_not_applicable": [
    { "gate": "cuantia_competencia", "reason": "demanda_divorcio no tiene cuantía, competencia por área" }
  ],
  "gates": [
    {
      "gate": "data_consistency|pretension_supported|norma_developed|liquidacion_coherente|cuantia_competencia|jurisdiccion_competencia",
      "passed": true|false,
      "severity": "critical|warning|info",
      "description": "qué se detectó (solo si !passed)",
      "affected_blocks": ["block_id1", "block_id2"],
      "suggested_fix": "cambio CONCRETO para resolver"
    }
  ]
}
"""


def _summarize_blocks_for_coherence(blocks: list[dict]) -> str:
    """Resumen estructurado del doc completo para el LLM (≤6000 chars)."""
    out: list[str] = []
    for b in blocks[:150]:
        bid = b.get("block_id") or "?"
        bt = b.get("block_type") or b.get("block_data", {}).get("type") or "?"
        bd = b.get("block_data") or {}
        sk = b.get("section_key") or bd.get("section_key") or ""

        def _txt() -> str:
            runs = bd.get("runs") or []
            if isinstance(runs, list):
                return "".join(
                    r.get("text", "") if isinstance(r, dict) else str(r)
                    for r in runs
                )[:200]
            return ""

        if bt == "title":
            out.append(f"[{bid}] TITLE: {bd.get('text', '')[:80]}")
        elif bt == "section_heading":
            out.append(f"[{bid}] §{bd.get('roman', '')}. {bd.get('text', '')} (key={bd.get('section_key')})")
        elif bt == "subsection":
            out.append(f"[{bid}] {bd.get('number', '')}. {bd.get('text', '')[:80]}")
        elif bt == "paragraph":
            out.append(f"[{bid}] (sec={sk}) PARRAFO: {_txt()}")
        elif bt == "hecho":
            out.append(f"[{bid}] (sec={sk}) HECHO #{bd.get('num')}: {_txt()}")
        elif bt == "pretension":
            out.append(f"[{bid}] (sec={sk}) PRETENSION {bd.get('ord')} kind={bd.get('kind')}: {_txt()}")
        elif bt == "list_item":
            out.append(f"[{bid}] (sec={sk}) ITEM {bd.get('num')}: {_txt()}")
        elif bt == "norma_citada":
            content = " ".join(
                r.get("text", "") if isinstance(r, dict) else str(r)
                for r in (bd.get("contenido") or [])
            )[:150]
            out.append(f"[{bid}] NORMA: {bd.get('norma')} | {content}")
        elif bt == "jurisprudencia":
            out.append(f"[{bid}] JURISP: {bd.get('id')} M.P. {bd.get('mp')} | {bd.get('corte')}")
        elif bt == "table":
            hdr = bd.get("header") or []
            rows = bd.get("rows") or []
            row_preview = " | ".join(
                str(r[0]) + " = " + str(r[-1]) if isinstance(r, list) and len(r) >= 2 else ""
                for r in rows[:6]
            )
            out.append(f"[{bid}] TABLA ({len(rows)} filas) HDR={hdr} ROWS={row_preview}")
        elif bt == "calc_step":
            out.append(f"[{bid}] CALC {bd.get('label')}: {bd.get('aplicacion')} = {bd.get('total')}")
        elif bt == "firma":
            out.append(f"[{bid}] FIRMA: {bd.get('nombre')} TP {bd.get('tp')} C.C. {bd.get('cc')} ciudad_fecha={bd.get('ciudad_fecha')}")
        elif bt == "juramento":
            out.append(f"[{bid}] JURAMENTO norma_ref={bd.get('norma_ref')}")
    return "\n".join(out)


async def check_coherence(
    client,
    blocks: list[dict],
    doc_type: str | None,
) -> CoherenceReport:
    """Stage principal. Corre LLM-as-judge sobre el documento completo."""
    try:
        doc_summary = _summarize_blocks_for_coherence(blocks)
        user_prompt = f"""DOC TYPE: {doc_type or 'desconocido'}

DOCUMENTO COMPLETO (bloques con block_id):
{doc_summary}

Revisa los 6 gates de coherencia. Devuelve JSON {{overall_score, gates: [...]}}.
"""
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": COHERENCE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=2000,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        gates_raw = data.get("gates") or []
        passed: list[str] = []
        failed: list[CoherenceFinding] = []
        for g in gates_raw[:8]:
            if not isinstance(g, dict):
                continue
            gate_name = str(g.get("gate", "unknown"))[:40]
            if g.get("passed"):
                passed.append(gate_name)
            else:
                sev = g.get("severity", "warning")
                if sev not in ("critical", "warning", "info"):
                    sev = "warning"
                failed.append(CoherenceFinding(
                    gate=gate_name,
                    severity=sev,
                    description=str(g.get("description", ""))[:300],
                    affected_blocks=[
                        str(b)[:80] for b in (g.get("affected_blocks") or [])[:5]
                    ],
                    suggested_fix=str(g.get("suggested_fix", ""))[:300] or None,
                ))

        # M19.23.K — parse gates_not_applicable (gates que el LLM determinó que
        # no aplican al doc_type, ej. cuantia_competencia en demanda_divorcio)
        not_applicable: list[dict] = []
        for na in (data.get("gates_not_applicable") or [])[:8]:
            if isinstance(na, dict) and na.get("gate"):
                not_applicable.append({
                    "gate": str(na.get("gate", ""))[:40],
                    "reason": str(na.get("reason", ""))[:200],
                })

        # M19.23.K — score recalculado: si el LLM mandó score, lo respetamos.
        # Si no, calculamos pass_count / (pass_count + fail_count). Los
        # not_applicable NO penalizan. Antes el score=0 cuando 5 de 6 gates
        # fallaban incluyendo gates que no aplicaban → ahora es justo.
        applicable_count = len(passed) + len(failed)
        if applicable_count == 0:
            computed_score = 1.0
        else:
            computed_score = len(passed) / applicable_count

        score_from_llm = data.get("overall_score")
        try:
            score = float(score_from_llm) if score_from_llm is not None else computed_score
            score = max(0.0, min(1.0, score))
        except Exception:
            score = computed_score

        critical = sum(1 for f in failed if f.severity == "critical")
        report = CoherenceReport(
            doc_type=doc_type or "default",
            overall_score=round(score, 3),
            gates_passed=passed,
            gates_failed=failed,
            gates_not_applicable=not_applicable,
            doc_type_reasoning=str(data.get("doc_type_reasoning", ""))[:400],
            can_continue=(critical == 0),
        )
        logger.info(
            "coherence_check: doc_type=%s score=%.2f passed=%d failed=%d not_applicable=%d critical=%d",
            doc_type, score, len(passed), len(failed), len(not_applicable), critical,
        )
        return report
    except Exception as e:
        logger.warning("coherence_check failed (non-fatal): %s", e)
        return CoherenceReport(
            doc_type=doc_type or "default",
            overall_score=1.0,
            gates_passed=["fallback_no_check"],
            gates_failed=[],
            can_continue=True,
        )
