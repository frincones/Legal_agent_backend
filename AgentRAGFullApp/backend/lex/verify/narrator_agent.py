"""Sprint M19.5 · NarratorAgent — genera prosa en español estilo Claude.

Llamadas cortas a gpt-4o-mini que producen párrafos narrativos para el
ThoughtStream del usuario. Reemplaza los logs hardcoded por prosa
natural indistinguible de Claude.

Momentos de invocación (orchestrator):
  1. intro       : "Voy a verificar las normas, jurisprudencia..."
  2. post_preflight: "Importante: Detecté X observaciones..."
  3. mid_verification: "Confirmé las normas constitucionales. Ahora..."
  4. post_verification: "✓ Verificación completa. 19/21 confirmadas..."
  5. synthesis   : Resumen final con corrections aplicadas (estilo Claude)

Costo: ~$0.0005/call × 4-5 calls = $0.002-0.003 por documento.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

NARRATOR_MODEL = "gpt-4o-mini"
NARRATOR_MAX_TOKENS = 400
NARRATOR_TEMPERATURE = 0.6


SYSTEM_PROMPT_BASE = """Eres un agente legal colombiano que NARRA en primera persona lo que está haciendo, en español natural y profesional. Estilo Claude.ai.

REGLAS DE ESTILO:
- Habla en primera persona ("Voy a verificar...", "Confirmé que...", "Detecté que...")
- Tono profesional pero conversacional (NO logs técnicos, NO emojis excesivos)
- Markdown soportado: **bold**, listas numeradas (1. 2. 3.), bullets (- ), `código inline`
- Párrafos cortos (2-4 frases máximo por párrafo)
- Termina con una transición clara cuando hay siguientes pasos ("Voy a continuar con...")
- NO repitas información obvia. Sé conciso.
- NO uses encabezados (#, ##) — usa **bold** para énfasis
- NO uses emojis en el inicio de cada bullet (los bullets son `- ` o `1. `)

LONGITUD:
- Mensajes intermedios: 1-2 párrafos
- Síntesis final: hasta 4 párrafos con corrections importantes

Output: SOLO el texto narrativo en markdown, sin envoltorios JSON ni explicaciones."""


PROMPTS_BY_MOMENT = {
    "intro": """El usuario me pidió generar un documento legal. Voy a empezar verificando las referencias normativas y jurisprudenciales antes de redactar.

Contexto del prompt del usuario:
{intent_preview}

Tipo de documento detectado: {doc_type} ({jurisdiccion}, {materia})

Total de citas a verificar (estimado): {n_citations}

Genera un párrafo de introducción (1 párrafo, 2-3 frases) en primera persona explicando qué vas a hacer. NO listes las citas individualmente. Termina con una transición ("Voy a empezar por...").""",

    "post_preflight": """Acabo de hacer un análisis previo del prompt del usuario y detecté las siguientes observaciones jurídicas:

{findings_json}

Genera un mensaje narrativo (1-2 párrafos) que:
1. Empiece con "**Importante**: detecté las siguientes observaciones en tu solicitud:"
2. Liste cada finding como bullet, explicando brevemente el problema y la sugerencia (si hay)
3. Termine indicando que vas a continuar con la verificación de las citas restantes y aplicar las correcciones""",

    "mid_verification": """He completado la verificación de un bloque de citas. Aquí el resumen:

{summary_json}

Genera un párrafo corto (2-3 frases) que mencione cuántas citas verificaste y qué fuentes principales se confirmaron. Termina con una transición a la siguiente fase si la hay.""",

    "post_verification": """Acabo de terminar la verificación completa de todas las citas:

Total citas: {total}
Verificadas: {verified}
No encontradas: {not_found}
Con sugerencias de corrección del Judge: {corrections}
Con notas legales (derogadas/modificadas): {legal_notes}

Lista de corrections sugeridas:
{corrections_list}

Lista de notas legales:
{notes_list}

Genera un mensaje sintético (2-3 párrafos) que:
1. Indique el resultado global de la verificación
2. Liste las correcciones sugeridas que el usuario debe considerar (con bullets)
3. Liste las notas de vigencia/modificación detectadas
4. Termine indicando que vas a proceder con la redacción del documento""",

    "synthesis": """El documento ya está redactado y verificado. Genera el mensaje final que el agente le da al usuario, estilo Claude:

Resultado: {result_summary}

Correcciones aplicadas en el documento: {corrections_applied}

Genera un mensaje final (3-4 párrafos) que:
1. Indique que el documento está listo
2. Liste como buena práctica las correcciones importantes que se incorporaron al verificar las fuentes (numeradas: 1. 2. 3.)
3. Indique las fuentes oficiales usadas (gov.co)
4. Sugiera próximos pasos (completar datos en blanco, revisar liquidación, etc.)

Usa formato como en este ejemplo:
\"La demanda está lista. Como buena práctica profesional, debo señalar las correcciones importantes que se incorporaron al verificar las fuentes oficiales:

1. **Salario integral inconsistente.** $4.850.000 no puede ser 'salario integral': el piso legal del art. 132 CST es ≥13 SMMLV...

2. ...\""""
}


@dataclass
class NarrationResult:
    text: str
    duration_ms: int = 0
    tokens_used: int = 0
    error: Optional[str] = None


async def narrate(
    client,
    moment: str,
    context: dict[str, Any],
    enabled: Optional[bool] = None,
) -> NarrationResult:
    """Genera un párrafo narrativo para el thought stream.

    Args:
        client: openai AsyncClient (gpt-4o-mini)
        moment: 'intro' | 'post_preflight' | 'mid_verification' | 'post_verification' | 'synthesis'
        context: dict con keys requeridas según el moment (ver PROMPTS_BY_MOMENT)
        enabled: bypass con False (usa fallback hardcoded)

    Returns:
        NarrationResult con text en markdown (puede ser vacío si error)
    """
    started = time.time()

    if enabled is False or client is None or moment not in PROMPTS_BY_MOMENT:
        # Fallback hardcoded
        return NarrationResult(
            text=_fallback_narration(moment, context),
            duration_ms=int((time.time() - started) * 1000),
        )

    try:
        user_prompt = PROMPTS_BY_MOMENT[moment].format(**_safe_format_context(moment, context))
    except KeyError as e:
        logger.warning("narrator missing context key for %s: %s", moment, e)
        return NarrationResult(
            text=_fallback_narration(moment, context),
            duration_ms=int((time.time() - started) * 1000),
            error=f"missing_context:{e}",
        )

    try:
        resp = await client.chat.completions.create(
            model=NARRATOR_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT_BASE},
                {"role": "user", "content": user_prompt},
            ],
            temperature=NARRATOR_TEMPERATURE,
            max_tokens=NARRATOR_MAX_TOKENS,
        )
        text = (resp.choices[0].message.content or "").strip()
        tokens = (resp.usage.total_tokens if resp.usage else 0) if resp else 0
        return NarrationResult(
            text=text or _fallback_narration(moment, context),
            duration_ms=int((time.time() - started) * 1000),
            tokens_used=tokens,
        )
    except Exception as e:
        logger.warning("narrator LLM call failed (%s): %s", moment, e)
        return NarrationResult(
            text=_fallback_narration(moment, context),
            duration_ms=int((time.time() - started) * 1000),
            error=str(e)[:200],
        )


def _safe_format_context(moment: str, context: dict[str, Any]) -> dict[str, Any]:
    """Asegura que las keys del prompt existan, rellena defaults safe."""
    defaults = {
        "intro": {"intent_preview": "...", "doc_type": "documento_legal",
                  "jurisdiccion": "general", "materia": "general", "n_citations": "varias"},
        "post_preflight": {"findings_json": "[]"},
        "mid_verification": {"summary_json": "{}"},
        "post_verification": {
            "total": 0, "verified": 0, "not_found": 0,
            "corrections": 0, "legal_notes": 0,
            "corrections_list": "(ninguna)", "notes_list": "(ninguna)",
        },
        "synthesis": {"result_summary": "{}", "corrections_applied": "(ninguna)"},
    }
    merged = {**defaults.get(moment, {}), **context}
    # Stringify any dict/list
    for k, v in merged.items():
        if isinstance(v, (dict, list)):
            merged[k] = json.dumps(v, ensure_ascii=False, indent=2, default=str)[:1500]
    return merged


def _fallback_narration(moment: str, context: dict[str, Any]) -> str:
    """Plantillas hardcoded si el LLM falla. Mejor que silencio."""
    if moment == "intro":
        n = context.get("n_citations", "varias")
        return f"Voy a verificar las {n} referencias normativas y jurisprudenciales mencionadas en tu solicitud antes de redactar el documento. Empezaré por las normas constitucionales y luego pasaré a la jurisprudencia."
    if moment == "post_preflight":
        findings = context.get("findings", [])
        if not findings:
            return "**Verifiqué la coherencia jurídica del prompt.** No detecté errores obvios. Procedo a verificar las citas."
        n = len(findings) if isinstance(findings, list) else "varias"
        return f"**Importante**: detecté {n} observaciones en tu solicitud que conviene revisar. Voy a continuar con la verificación de las citas e incorporar las correcciones donde aplique."
    if moment == "post_verification":
        v = context.get("verified", 0)
        t = context.get("total", 0)
        return f"✓ Verificación completa: **{v} de {t} citas confirmadas**. Procedo a redactar el documento con las correcciones aplicadas."
    if moment == "synthesis":
        return "El documento está listo. Revisa el panel de Audit para ver el detalle de cada cita verificada y sus fuentes oficiales."
    return ""
