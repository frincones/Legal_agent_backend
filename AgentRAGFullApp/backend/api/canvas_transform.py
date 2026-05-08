"""Sprint 3 · POST /v1/canvas/transform

Transforma una selección de texto del Canvas según una acción IA:
- improve    → reescribe con tono profesional
- formalize  → lenguaje jurídico técnico
- summarize  → condensa en 1-2 oraciones
- cite       → añade jurisprudencia colombiana relacionada (citas reales)

Llamado desde el bubble menu inline del CanvasEditor cuando el abogado
selecciona texto y elige una acción.
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm
from utils.llm import llm_generate

router = APIRouter(prefix="/v1/canvas", tags=["canvas"])


Action = Literal["improve", "formalize", "summarize", "cite"]


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

    try:
        result = await llm_generate(
            prompt=user_msg,
            model="gpt-4o-mini",
            system_prompt=sys_prompt,
            temperature=0.3 if body.action in ("improve", "formalize", "cite") else 0.1,
            max_tokens=1_500,
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
