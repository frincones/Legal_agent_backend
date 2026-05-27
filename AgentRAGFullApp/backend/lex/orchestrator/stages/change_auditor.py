"""M19.17.C — Stage Change Auditor: revisa cada edit del documento como un
abogado litigante senior colombiano antes de "aprobarlo".

Tras cada modificación (PATCH inline, replace_selection, update_block via chat,
regenerate_section), este stage corre en background y produce findings sobre
8 dimensiones de calidad jurídica:

  1. Coherencia interna   — datos, fechas, nombres consistentes entre secciones
  2. Dependencias         — normas o jurisprudencia que sustentaban el original
  3. Riesgos legales      — exposición del cliente o debilitamiento de pretensión
  4. Vacíos normativos    — afirmaciones sin sustento legal tras el cambio
  5. Pretensiones derivadas — viabilidad de las pretensiones que dependen
  6. Liquidación          — coherencia con la tabla de conceptos
  7. Procesales           — cuantía, competencia, caducidad, prescripción
  8. Buenas prácticas     — tono, terminología, citas profesionales

Output: { coherence_score, findings: [{block_id, dimension, severity, issue,
suggested_change}], summary }

Reusa OpenAI client del módulo `utils.llm`. Costo: gpt-4o-mini con ~2-3K tokens
de contexto. No bloquea el endpoint que lo invoca (background task FastAPI).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Literal

logger = logging.getLogger(__name__)


CHANGE_AUDITOR_SYSTEM_PROMPT = """Eres un ABOGADO LITIGANTE SENIOR colombiano con 25+ años de experiencia,
especialmente entrenado en revisar memoriales antes de firma. Tu trabajo NO es
re-redactar, sino IDENTIFICAR riesgos y posibles problemas en un cambio que
un colega aplicó al documento.

Te pasan:
  1. El documento completo (resumen de bloques con block_id).
  2. El bloque que fue editado (texto antes y después del cambio).
  3. La instrucción / mensaje del usuario que motivó el cambio.

Tu tarea es revisar el cambio en estas 8 DIMENSIONES y devolver findings:

  1. COHERENCIA INTERNA
     - ¿Hay otros bloques que mencionan los mismos datos (fechas, nombres,
       montos, cédulas) que ahora quedaron incoherentes?
     - Ej: cambias fecha del hecho 1, pero el hecho 5 dice "tres semanas
       después del 15 de marzo" → INCOHERENCIA.

  2. DEPENDENCIAS NORMATIVAS / JURISPRUDENCIALES
     - ¿El texto original citaba una norma o sentencia que ya no aparece
       después del cambio y que sustentaba una pretensión?
     - ¿Quedaron menciones a "art. X CST" sin la cita correspondiente?

  3. RIESGOS LEGALES
     - ¿El cambio expone al cliente (admisión perjudicial, renuncia tácita
       de derechos, datos sensibles innecesarios)?
     - ¿Debilita una pretensión clave o reduce la cuantía sin justificación?

  4. VACÍOS NORMATIVOS
     - ¿Hay afirmaciones jurídicas tras el cambio que ya no tienen sustento
       en una norma o jurisprudencia citada en el documento?

  5. PRETENSIONES DERIVADAS
     - Si el hecho cambió, ¿siguen siendo viables las pretensiones que se
       derivan de él? (ej: cambias "despido sin justa causa" por "renuncia
       voluntaria" → la pretensión de indemnización Art. 64 CST pierde
       sustento.)

  6. LIQUIDACIÓN / CÁLCULOS
     - Si cambió un salario, fecha de ingreso/terminación o años de servicio,
       ¿la tabla de liquidación quedó inconsistente?

  7. PROCESALES
     - ¿El cambio afecta la cuantía (mayor/menor cuantía → competencia)?
     - ¿Modifica un cómputo de caducidad o prescripción?
     - ¿Hace que el juez competente cambie?

  8. BUENAS PRÁCTICAS FORENSES
     - Tono solemne pero claro
     - Terminología jurídica correcta
     - Citas con formato (Art. X de Ley Y/AAAA, Sentencia ID/AAAA M.P. Nombre)
     - Evitar coloquialismos, valoraciones subjetivas, redundancias

REGLAS DE OUTPUT:
- Si no encuentras problemas significativos, devuelve findings: [] y
  coherence_score >= 0.9.
- NO inventes problemas para "demostrar trabajo". Si todo está bien, dilo.
- Cada finding debe ser ACCIONABLE: block_id afectado + qué cambio sugieres
  literal. Evita "revisar X" sin sugerencia concreta.
- severity:
    "critical": riesgo procesal o de derecho de fondo. El usuario DEBE corregir.
    "warning":  inconsistencia importante pero no fatal.
    "info":     mejora estilística o recordatorio.
- Máximo 5 findings (los más importantes).
- coherence_score: 0.0–1.0 (1.0 = perfecto, 0.0 = inconsistencia grave).

OUTPUT (SOLO JSON válido, sin markdown):
{
  "coherence_score": 0.0,
  "summary": "una frase resumiendo el veredicto",
  "findings": [
    {
      "block_id": "<id del bloque afectado, NO del bloque editado>",
      "dimension": "coherencia_interna|dependencias|riesgos|vacios|pretensiones|liquidacion|procesales|buenas_practicas",
      "severity": "critical|warning|info",
      "issue": "qué problema detectaste, en 1-2 frases",
      "suggested_change": "cambio textual sugerido para resolver"
    }
  ]
}
"""


Severity = Literal["critical", "warning", "info"]


def _summarize_blocks_for_audit(blocks: list[dict[str, Any]], edited_block_id: str) -> str:
    """Resumen compacto de bloques para el LLM, marcando el bloque editado."""
    out: list[str] = []
    for b in blocks[:120]:
        bid = b.get("block_id") or "?"
        bt = b.get("block_type") or "?"
        bd = b.get("block_data") or {}
        marker = " <<EDITADO>>" if bid == edited_block_id else ""

        if bt == "title":
            out.append(f"[{bid}]{marker} TITLE: {bd.get('text', '')[:100]}")
        elif bt == "section_heading":
            out.append(
                f"[{bid}]{marker} §{bd.get('roman', '')}. {bd.get('text', '')} (key={bd.get('section_key')})"
            )
        elif bt == "subsection":
            out.append(f"[{bid}]{marker} {bd.get('number', '')}. {bd.get('text', '')[:80]}")
        elif bt == "paragraph":
            text = "".join(r.get("text", "") for r in bd.get("runs", []))[:280]
            out.append(f"[{bid}]{marker} PARRAFO: {text}")
        elif bt == "hecho":
            text = "".join(r.get("text", "") for r in bd.get("runs", []))[:280]
            out.append(f"[{bid}]{marker} HECHO #{bd.get('num')}: {text}")
        elif bt == "pretension":
            text = "".join(r.get("text", "") for r in bd.get("runs", []))[:280]
            out.append(f"[{bid}]{marker} PRETENSION {bd.get('ord')}: {text}")
        elif bt == "list_item":
            text = "".join(r.get("text", "") for r in bd.get("runs", []))[:200]
            out.append(f"[{bid}]{marker} ITEM {bd.get('num')}: {text}")
        elif bt == "norma_citada":
            out.append(f"[{bid}]{marker} NORMA: {bd.get('norma', '?')}")
        elif bt == "jurisprudencia":
            out.append(
                f"[{bid}]{marker} JURISP: {bd.get('id', '?')} M.P. {bd.get('mp', '?')}"
            )
        elif bt == "table":
            hdr = bd.get("header") or []
            rows_n = len(bd.get("rows") or [])
            out.append(f"[{bid}]{marker} TABLA: {hdr} ({rows_n} filas)")
        elif bt == "calc_step":
            out.append(
                f"[{bid}]{marker} CALC {bd.get('label')}: {bd.get('total', '?')}"
            )
        elif bt == "firma":
            out.append(f"[{bid}]{marker} FIRMA: {bd.get('nombre', '?')} TP {bd.get('tp', '?')}")
        elif bt == "juramento":
            out.append(f"[{bid}]{marker} JURAMENTO")
    return "\n".join(out)


def _block_to_text(block: dict[str, Any] | None) -> str:
    """Convierte un bloque a texto plano para mostrar 'antes' / 'después'."""
    if not block:
        return ""
    bd = block.get("block_data") or block
    bt = bd.get("type") or block.get("block_type")
    if bt in ("paragraph", "hecho", "pretension", "list_item"):
        return "".join(r.get("text", "") for r in bd.get("runs", []))
    if bt in ("title", "section_heading", "subsection"):
        return bd.get("text", "")
    if bt == "norma_citada":
        return f"NORMA: {bd.get('norma', '')}"
    if bt == "jurisprudencia":
        return f"{bd.get('id', '')} M.P. {bd.get('mp', '')}"
    if bt == "firma":
        return f"{bd.get('nombre', '')} · TP {bd.get('tp', '')} · CC {bd.get('cc', '')}"
    return json.dumps(bd, ensure_ascii=False)[:200]


async def audit_change(
    client,
    all_blocks: list[dict[str, Any]],
    edited_block_id: str,
    before_block: dict[str, Any] | None,
    after_block: dict[str, Any] | None,
    user_instruction: str = "",
) -> dict[str, Any]:
    """Corre el LLM-as-judge sobre un cambio del documento.

    Devuelve {coherence_score, summary, findings: [...]}.
    En caso de error devuelve {coherence_score: 1.0, findings: [],
    summary: "audit_unavailable"} (no rompe el flujo del usuario).
    """
    try:
        before_text = _block_to_text(before_block)
        after_text = _block_to_text(after_block)
        doc_summary = _summarize_blocks_for_audit(all_blocks, edited_block_id)

        user_prompt = f"""DOCUMENTO COMPLETO (bloques con block_id, <<EDITADO>> marca el cambiado):
{doc_summary}

CAMBIO REALIZADO en bloque [{edited_block_id}]:
ANTES:
\"\"\"
{before_text[:1500]}
\"\"\"
DESPUÉS:
\"\"\"
{after_text[:1500]}
\"\"\"

INSTRUCCIÓN/MENSAJE QUE MOTIVÓ EL CAMBIO:
\"\"\"
{user_instruction[:600] if user_instruction else "(edit inline directo del usuario, sin instrucción de texto)"}
\"\"\"

Audita este cambio en las 8 dimensiones y devuelve JSON {{coherence_score, summary, findings: [...]}}."""

        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": CHANGE_AUDITOR_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=1800,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        # Sanitizar
        findings = data.get("findings") or []
        if not isinstance(findings, list):
            findings = []
        clean_findings: list[dict[str, Any]] = []
        for f in findings[:5]:
            if not isinstance(f, dict):
                continue
            sev = f.get("severity", "info")
            if sev not in ("critical", "warning", "info"):
                sev = "info"
            clean_findings.append({
                "block_id": str(f.get("block_id", ""))[:80],
                "dimension": str(f.get("dimension", "buenas_practicas"))[:40],
                "severity": sev,
                "issue": str(f.get("issue", ""))[:400],
                "suggested_change": str(f.get("suggested_change", ""))[:600],
            })
        score = data.get("coherence_score", 1.0)
        try:
            score = float(score)
            if score < 0:
                score = 0.0
            if score > 1:
                score = 1.0
        except Exception:
            score = 1.0
        return {
            "coherence_score": score,
            "summary": str(data.get("summary", ""))[:300],
            "findings": clean_findings,
        }
    except Exception as e:
        logger.warning("change_auditor failed (non-fatal): %s", e)
        return {
            "coherence_score": 1.0,
            "summary": "audit_unavailable",
            "findings": [],
        }
