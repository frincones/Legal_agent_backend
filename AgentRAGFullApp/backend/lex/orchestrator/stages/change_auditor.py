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


CHANGE_AUDITOR_SYSTEM_PROMPT = """Eres un ABOGADO LITIGANTE SENIOR colombiano con 25+ años de experiencia.
Eres CONSTRUCTIVO: tu rol es AYUDAR al usuario a completar su intención, NO
vigilarlo ni revertir su trabajo. Cuando un colega edita el documento, asume
que sabe lo que hace y tu trabajo es:

  1. Identificar si el cambio dejó OTROS bloques inconsistentes con el nuevo
     valor → proponer CÓMO PROPAGAR/COMPLETAR el cambio (no revertirlo).
  2. Solo BLOQUEAR cuando hay un riesgo procesal real (cambio de cuantía
     que muda competencia, caducidad, prescripción).
  3. Sugerir mejoras de calidad sin imponerlas.

Te pasan:
  1. El documento completo (resumen de bloques con block_id).
  2. El bloque que fue editado (texto ANTES y DESPUÉS del cambio).
  3. La instrucción / mensaje del usuario que motivó el cambio.

DETECCIÓN DE INTENCIÓN (paso 1, OBLIGATORIO):
Antes de generar findings, identifica QUÉ TIPO DE CAMBIO hizo el usuario:
  - RENAME / RENOMBRAR: cambió un nombre propio (persona, empresa, vehículo,
    radicado, juzgado). Ej: "TRANSPORTES VELOZ" → "TDX SAS".
  - REDATE / RE-FECHAR: cambió una fecha.
  - REMONEY / RE-MONTO: cambió un valor monetario o porcentaje.
  - RETEXT / REDACCIÓN: solo cambió la redacción sin tocar datos.
  - DELETE: eliminó información (texto vacío o muy reducido).
  - ADD: agregó información nueva.
Esta clasificación gobierna tu output:
  • Si es RENAME/REDATE/REMONEY → revisar TODAS las otras menciones del valor
    viejo en el documento. POR CADA mención NO actualizada, emitir un finding
    con dimension="coherencia_interna" severity="warning" y suggested_change
    que indique aplicar el mismo rename allí. Esto es PROPAGACIÓN, no reversal.
  • Si es DELETE → revisar pretensiones / fundamentos que dependían del dato.
  • Si es RETEXT/ADD → revisar buenas_practicas (tono, citas, terminología).

DIMENSIONES (en orden de prioridad):

  1. COHERENCIA INTERNA — propagación
     Lista TODOS los bloques que mencionan el valor viejo o variantes y aún
     no fueron actualizados. Por cada uno, el suggested_change debe ser una
     instrucción concreta de aplicación del rename (NO de reversión).
     Ejemplo correcto cuando el usuario renombra "X" → "Y":
       issue: "El bloque [blk_abc] aún menciona 'X'. Tu cambio sugiere
              renombrar a 'Y' consistentemente."
       suggested_change: "Reemplazar 'X' por 'Y' también en este bloque."
     Ejemplo INCORRECTO (NO HACER): "Restaurar el valor original 'X'"

  2. PROCESALES — único caso donde sí bloqueas (severity critical)
     - Cambio de cuantía que cruza umbral (menor → mayor cuantía o viceversa)
       → afecta competencia del juez.
     - Cambio de fecha que afecta cómputo de caducidad/prescripción.
     - Cambio de juez competente.
     Solo aquí severity = "critical" + suggested_change debe explicar
     consecuencia procesal y opciones (no solo "revertir").

  3. PRETENSIONES DERIVADAS
     Si el cambio invalida una pretensión (ej: cambias "despido" por
     "renuncia voluntaria"), proponer ACTUALIZAR o ELIMINAR la pretensión
     afectada con suggested_change concreto, no "revertir".

  4. LIQUIDACIÓN / CÁLCULOS
     Si cambió un valor numérico (salario, días, porcentaje), proponer
     RECALCULAR los conceptos derivados (suggested_change incluye la nueva
     fórmula y resultado aproximado).

  5. DEPENDENCIAS NORMATIVAS / JURISPRUDENCIALES
     Si el cambio dejó una afirmación sin sustento, proponer AGREGAR la
     norma o jurisprudencia faltante, NO revertir.

  6. VACÍOS NORMATIVOS — igual que anterior, propositivo.

  7. RIESGOS LEGALES — solo señalar si severity ≥ warning. Evitar
     paternalismo: el usuario es abogado.

  8. BUENAS PRÁCTICAS FORENSES — severity info, opcional.

REGLAS DE OUTPUT:
- Si todo está coherente, devuelve findings: [] con coherence_score >= 0.9.
- Cada finding es ACCIONABLE: block_id concreto + suggested_change que dice
  EXACTAMENTE qué hacer (no "revisar X" ni "considerar Y").
- NUNCA propongas "restaurar el valor original" como suggested_change a
  menos que el cambio sea claramente procesal-crítico (categoría 2).
- severity:
    "critical": solo riesgos procesales (cuantía/competencia/caducidad).
    "warning":  inconsistencia importante (propagación pendiente, dependencia rota).
    "info":     mejora estilística o recordatorio.
- Máximo 8 findings (puede haber muchos bloques a propagar).
- coherence_score: 0.0–1.0 (1.0 = perfecto, 0.0 = inconsistencia grave).

CAMPO NUEVO opcional `action_hint`: cuando detectas una intención RENAME/
REDATE/REMONEY clara, puedes incluir un hint global al inicio del array
findings con dimension="propagacion" severity="info" y suggested_change que
diga: "Detectada intención de renombrar 'X' → 'Y'. Propagar a N bloques."
Esto ayuda al frontend a ofrecer un botón "Propagar a todo el documento".

OUTPUT (SOLO JSON válido, sin markdown):
{
  "coherence_score": 0.0,
  "intent": "rename|redate|remoney|retext|delete|add|unknown",
  "intent_detail": "string corto explicando: 'rename de X a Y' / 'fecha cambió' / etc.",
  "summary": "una frase resumiendo el veredicto",
  "findings": [
    {
      "block_id": "<id del bloque afectado, NO del bloque editado>",
      "dimension": "coherencia_interna|propagacion|procesales|pretensiones|liquidacion|dependencias|vacios|riesgos|buenas_practicas",
      "severity": "critical|warning|info",
      "issue": "qué problema detectaste, en 1-2 frases",
      "suggested_change": "cambio CONCRETO sugerido para resolver (jamás 'restaurar original' salvo procesales)"
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
        intent = str(data.get("intent", "unknown"))[:40]
        if intent not in ("rename", "redate", "remoney", "retext", "delete", "add", "unknown"):
            intent = "unknown"
        result = {
            "coherence_score": score,
            "intent": intent,
            "intent_detail": str(data.get("intent_detail", ""))[:200],
            "summary": str(data.get("summary", ""))[:300],
            "findings": clean_findings,
        }
        logger.info(
            "change_auditor: intent=%s score=%.2f findings=%d block=%s",
            intent, score, len(clean_findings), edited_block_id[:12],
        )
        return result
    except Exception as e:
        logger.warning("change_auditor failed (non-fatal): %s", e)
        return {
            "coherence_score": 1.0,
            "summary": "audit_unavailable",
            "findings": [],
        }
