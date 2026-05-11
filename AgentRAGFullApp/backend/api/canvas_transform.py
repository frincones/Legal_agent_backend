"""Sprint 3 · POST /v1/canvas/transform

Transforma una selección de texto del Canvas según una acción IA:
- improve     → reescribe con tono profesional
- formalize   → lenguaje jurídico técnico
- summarize   → condensa en 1-2 oraciones
- cite        → añade jurisprudencia colombiana relacionada (citas reales)
- suma        → genera el encabezado/proemio del escrito (TASK-S0-06)
- fundamentar → genera SOLO la sección de fundamentación de derecho
                con citas verificadas (TASK-S0-05, insight I-15)

Llamado desde el bubble menu inline del CanvasEditor cuando el abogado
selecciona texto y elige una acción, o desde botones del topbar para
las acciones que aplican al documento completo (suma, fundamentar).
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm
from utils.llm import llm_generate

router = APIRouter(prefix="/v1/canvas", tags=["canvas"])


Action = Literal[
    "improve", "formalize", "summarize", "cite",
    "suma", "fundamentar",
    # Sprint 3 · S3-02 · attack a counterparty argument with case-law
    "atacar",
]


class TransformRequest(BaseModel):
    action: Action
    text: str = Field(..., min_length=4, max_length=8_000)
    context_hint: Optional[str] = Field(
        None, description="Hint opcional sobre el caso (e.g. 'demanda laboral CST').",
        max_length=300,
    )


class TransformResponse(BaseModel):
    markdown: str
    action: Action
    chars_in: int
    chars_out: int


_SYSTEM_PROMPTS = {
    "improve": (
        "Eres un abogado colombiano experto en redacción legal. Reescribe el "
        "texto del usuario con tono profesional, claro y procesalmente "
        "correcto. Conserva la sustancia jurídica y los hechos. NO añadas "
        "información que no esté implícita en el texto original. Devuelve "
        "SOLO el texto reescrito en markdown, sin meta-comentarios."
    ),
    "formalize": (
        "Eres un abogado colombiano experto en redacción procesal. Reformula "
        "el texto con lenguaje jurídico técnico, usando expresiones formales "
        "del foro colombiano (e.g. 'esta parte', 'su Despacho', 'en gracia "
        "de discusión', 'a ruego', etc.) cuando aplique. Conserva sustancia "
        "y hechos. NO añadas pretensiones ni datos nuevos. Devuelve SOLO el "
        "texto reformulado en markdown."
    ),
    "summarize": (
        "Eres un abogado colombiano. Condensa el texto en 1-2 oraciones "
        "manteniendo la idea jurídica central. Devuelve SOLO el resumen en "
        "markdown, sin meta-comentarios."
    ),
    "cite": (
        "Eres un abogado colombiano experto en jurisprudencia. Añade al "
        "texto del usuario 1-2 referencias a jurisprudencia colombiana "
        "RELEVANTE (Corte Constitucional, Corte Suprema de Justicia, Consejo "
        "de Estado) usando el formato estándar (e.g. 'C.C., Sentencia "
        "T-388/2019', 'C.S.J., Sala Civil, Sent. SC-4801-2020'). NO inventes "
        "sentencias: si no estás 100% seguro de un número de sentencia o de "
        "su existencia, omítela. Conserva el texto original íntegro y añade "
        "las citas en una sección al final o entre paréntesis después del "
        "argumento que respaldan. Devuelve SOLO el texto en markdown."
    ),
    "suma": (
        "Eres un abogado colombiano experto en redacción procesal. Genera el "
        "ENCABEZADO/PROEMIO de un escrito jurídico colombiano a partir del "
        "contenido provisto por el usuario.\n\n"
        "Estructura esperada (1-2 párrafos breves):\n"
        "1. Saludo formal: 'HONORABLE JUEZ:' o el destinatario apropiado.\n"
        "2. Identificación del escrito: tipo (demanda, contestación, "
        "tutela…), partes, breve objeto.\n"
        "3. Referencia al juzgado/expediente si aparece en el texto.\n\n"
        "Reglas: NO inventes datos (nombres, NITs, expedientes, fechas). "
        "Solo usa lo que aparece en el texto. NO incluyas la fundamentación "
        "ni los hechos completos. Devuelve SOLO el encabezado en markdown, "
        "sin meta-comentarios."
    ),
    "atacar": (
        "Eres un abogado colombiano experto en defensa procesal. El "
        "usuario te pasará un párrafo o argumento de la CONTRAPARTE en "
        "un proceso judicial. Tu tarea: redactar un contra-argumento "
        "técnico y devastador.\n\n"
        "Estructura esperada (markdown, una sola sección):\n"
        "**Contra-argumento.** [Refutación clara, en 2-4 oraciones, que "
        "ataque la lógica jurídica del argumento contrario.]\n\n"
        "**Fundamento.** [Cita 1-2 normas o sentencias colombianas "
        "vigentes en formato estándar (e.g. T-388/2019, Art. 64 CST, "
        "Ley 1480/2011) que respalden tu refutación.]\n\n"
        "REGLAS CRÍTICAS:\n"
        "- NO inventes números de sentencia ni de norma.\n"
        "- Identifica si el argumento de la contraparte cita normas "
        "derogadas o jurisprudencia superada y, de ser así, señálalo.\n"
        "- Tono firme pero respetuoso; estilo procesal colombiano.\n"
        "- Devuelve SOLO el contra-argumento en markdown."
    ),
    "fundamentar": (
        "Eres un abogado colombiano experto en derecho sustantivo y "
        "jurisprudencia. Genera EXCLUSIVAMENTE la sección de "
        "'FUNDAMENTACIÓN DE DERECHO' (también llamada 'NORMAS APLICABLES' "
        "o 'DERECHO') de un escrito jurídico colombiano, basándote en los "
        "hechos y pretensiones que el usuario proporcione.\n\n"
        "Estructura esperada (en markdown):\n"
        "## FUNDAMENTACIÓN DE DERECHO\n"
        "1. **Marco constitucional** (si aplica): artículos relevantes de "
        "la Constitución Política de 1991.\n"
        "2. **Marco legal**: normas vigentes con número y año (e.g. "
        "'Ley 50 de 1990', 'Decreto 1572 de 2024'). Cita el artículo "
        "específico cuando sea pertinente.\n"
        "3. **Jurisprudencia aplicable**: 1-3 sentencias en formato "
        "estándar colombiano (T-XXX/AAAA, C-XXX/AAAA, SU-XXX/AAAA, "
        "SL-XXXXX-AAAA, SC-XXXXX-AAAA, SP-XXXXX-AAAA).\n\n"
        "REGLAS CRÍTICAS:\n"
        "- NO inventes números de sentencia ni de norma. Si no estás "
        "100% seguro, omite la cita o usa una formulación general.\n"
        "- NO incluyas hechos, pretensiones ni petitorio.\n"
        "- NO redactes el escrito completo: solo la sección de "
        "fundamentación.\n"
        "- Cada cita debe llevar su número exacto (T-388/2019, no "
        "'una sentencia reciente').\n"
        "- Devuelve SOLO el markdown de la sección, listo para insertar."
    ),
}


@router.post("/transform", response_model=TransformResponse)
async def transform(
    body: TransformRequest,
    principal: Principal = Depends(get_current_firm),
):
    sys_prompt = _SYSTEM_PROMPTS.get(body.action)
    if not sys_prompt:
        raise HTTPException(400, f"action inválida: {body.action}")

    user_msg = body.text
    if body.context_hint:
        user_msg = f"[Contexto: {body.context_hint}]\n\n{body.text}"

    # Modelo y parámetros por acción.
    # `fundamentar` y `atacar` exigen más calidad → gpt-4o.
    # `suma` y `summarize` pueden usar mini.
    HEAVY = {"fundamentar", "atacar"}
    model = "gpt-4o" if body.action in HEAVY else "gpt-4o-mini"
    temperature = (
        0.3 if body.action in ("improve", "formalize", "cite") else
        0.2 if body.action in HEAVY else
        0.1
    )
    max_tokens = 3_000 if body.action in HEAVY else 1_500

    try:
        result = await llm_generate(
            prompt=user_msg,
            model=model,
            system_prompt=sys_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            purpose=f"canvas_transform_{body.action}",
            session_id=str(principal.firm_id),
        )
    except Exception as e:
        raise HTTPException(502, f"LLM error: {e}")

    cleaned = (result or "").strip()
    if not cleaned:
        raise HTTPException(502, "respuesta vacía del modelo")

    # Quitar wrappers ```markdown ... ``` si el modelo los añade
    if cleaned.startswith("```"):
        first_nl = cleaned.find("\n")
        if first_nl > 0:
            cleaned = cleaned[first_nl + 1 :]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].rstrip()

    return TransformResponse(
        markdown=cleaned,
        action=body.action,
        chars_in=len(body.text),
        chars_out=len(cleaned),
    )
