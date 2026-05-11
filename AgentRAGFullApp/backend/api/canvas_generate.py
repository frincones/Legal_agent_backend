"""Sprint 3 · S3-01 · POST /v1/canvas/generate

Streaming-first endpoint for full-document drafting on the Canvas.
Different from /canvas/transform (single-pass action over text), this
endpoint produces a complete legal writ in markdown, streamed token by
token so the editor can show progress.

Body:
  {
    "matter_id":  "uuid",
    "doc_type":   "tutela" | "contestacion" | "demanda_laboral" | "derecho_peticion" | "recurso_apelacion" | "casacion" | "otro",
    "facts":      "string (free-form, the lawyer's account of the case)",
    "pretensions":"string (optional · what they ask for)",
    "context":    "string (optional · style guide / firm voice)"
  }

Streams NDJSON events:
  {"type":"start"}                                       (immediately)
  {"type":"text","delta":"..."}                          (one per chunk)
  {"type":"citation_detected","ref":"T-388/2019"}        (heuristic, derived from delta)
  {"type":"done","total_chars":N}                        (final)
  {"type":"error","message":"..."}                       (on failure)

The frontend appends `delta` to the canvas progressively and listens
for `citation_detected` to optimistically populate the sidebar.
"""

from __future__ import annotations

import json
import logging
import re
from typing import AsyncIterator, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm
from utils.llm import get_openai_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/canvas", tags=["canvas"])


DocType = Literal[
    "tutela",
    "contestacion",
    "demanda_laboral",
    "derecho_peticion",
    "recurso_apelacion",
    "casacion",
    "dictamen",
    "otro",
]


class GenerateRequest(BaseModel):
    matter_id: Optional[str] = None
    doc_type: DocType = "otro"
    facts: str = Field(..., min_length=10, max_length=8_000)
    pretensions: Optional[str] = Field(None, max_length=2_000)
    context: Optional[str] = Field(None, max_length=600)


# ────────────────────────────────────────────────────────────────────
# System prompts per doc_type
# ────────────────────────────────────────────────────────────────────

_DOC_GUIDES: dict[str, str] = {
    "tutela": (
        "Estás redactando una ACCIÓN DE TUTELA colombiana (Art. 86 C.P.). "
        "Estructura obligatoria:\n"
        "1. HONORABLE JUEZ + identificación del accionante\n"
        "2. I. HECHOS (numerados)\n"
        "3. II. DERECHOS VULNERADOS (referencias constitucionales: 11, 13, 23, 29, 49, 78, etc.)\n"
        "4. III. FUNDAMENTACIÓN DE DERECHO (jurisprudencia T-/SU-/C- vigente)\n"
        "5. IV. PRETENSIONES (numeradas)\n"
        "6. V. PRUEBAS\n"
        "7. VI. NOTIFICACIONES + firma."
    ),
    "contestacion": (
        "Estás redactando una CONTESTACIÓN DE DEMANDA. Estructura:\n"
        "1. SEÑOR JUEZ + identificación del apoderado\n"
        "2. I. CONTESTACIÓN A LOS HECHOS (numerados, uno por uno: cierto / no es cierto / no me consta)\n"
        "3. II. EXCEPCIONES PREVIAS Y DE FONDO\n"
        "4. III. FUNDAMENTOS DE DERECHO (citar normas vigentes y jurisprudencia)\n"
        "5. IV. PRUEBAS\n"
        "6. V. PETITORIO\n"
        "7. VI. ANEXOS + firma."
    ),
    "demanda_laboral": (
        "Estás redactando una DEMANDA ORDINARIA LABORAL (CST + Ley 50/1990 + Ley 789/2002). "
        "Estructura: HONORABLE JUEZ + partes + I. HECHOS + II. PRETENSIONES "
        "(cesantías, intereses, prima, vacaciones, indemnización, "
        "indexación) + III. FUNDAMENTOS DE DERECHO + IV. PRUEBAS."
    ),
    "derecho_peticion": (
        "Estás redactando un DERECHO DE PETICIÓN (Art. 23 C.P. y Ley 1755/2015). "
        "Estructura: ASUNTO + identificación + cuerpo respetuoso con la "
        "petición concreta + plazo legal de respuesta (15 días hábiles "
        "general) + firma."
    ),
    "recurso_apelacion": (
        "Estás redactando un RECURSO DE APELACIÓN. Estructura: HONORABLE "
        "MAGISTRADO + sustentación: I. ANTECEDENTES + II. PUNTOS DE "
        "INCONFORMIDAD (ataca específicamente cada error del fallo "
        "previo) + III. PRETENSIONES."
    ),
    "casacion": (
        "Estás redactando un RECURSO DE CASACIÓN ante la Corte Suprema "
        "de Justicia. Tono técnico extremo. Estructura: HONORABLES "
        "MAGISTRADOS + I. ANTECEDENTES + II. CARGOS (causales del CGP "
        "Art. 336 o CST Art. 60) + III. NORMAS VIOLADAS + IV. ALCANCE "
        "DE LA IMPUGNACIÓN."
    ),
    "dictamen": (
        "Estás redactando un DICTAMEN/OPINIÓN LEGAL para uso interno. "
        "Estructura: I. ANTECEDENTES + II. CONSULTA + III. ANÁLISIS "
        "JURÍDICO (citar normas y jurisprudencia) + IV. CONCLUSIONES."
    ),
    "otro": (
        "Estás redactando un escrito jurídico colombiano genérico. Usa "
        "encabezado formal, hechos numerados, fundamentación con citas "
        "de normas y jurisprudencia colombianas vigentes, y petitorio."
    ),
}

_SYSTEM_BASE = (
    "Eres un abogado colombiano senior con 15+ años de experiencia. "
    "Redactas escritos procesales con tono formal, técnico y respetuoso. "
    "Reglas críticas:\n"
    "- Cita ÚNICAMENTE jurisprudencia y normativa colombiana real y "
    "vigente. Si no estás 100% seguro de un número de sentencia o "
    "artículo, omítelo o usa una formulación general.\n"
    "- Formato de citas estándar: T-XXX/AAAA, C-XXX/AAAA, SU-XXX/AAAA, "
    "Ley XXXX/AAAA, Decreto XXXX/AAAA, Art. NN del CST/CGP/CCo/CC.\n"
    "- Devuelve SOLO el escrito en markdown. Sin meta-comentarios, sin "
    "preguntas al usuario, sin explicaciones de tu razonamiento.\n"
    "- Numera hechos y pretensiones."
)


_CITATION_RE = re.compile(
    r"\b(?:T|C|SU|SL|SC|SP)-?\d{1,5}[/\-]\d{2,4}\b|"
    r"\bLey\s+\d{1,5}\s+(?:de|del)\s*\d{2,4}\b|"
    r"\bDecreto(?:\s+(?:Ley|Reglamentario))?\s+\d{1,5}\s+(?:de|del)\s*\d{2,4}\b",
    re.IGNORECASE,
)


def _build_user_prompt(body: GenerateRequest) -> str:
    parts = [
        f"Tipo de escrito: {body.doc_type}",
        f"\nHechos del caso:\n{body.facts.strip()}",
    ]
    if body.pretensions:
        parts.append(f"\nPretensiones:\n{body.pretensions.strip()}")
    if body.context:
        parts.append(f"\nContexto / estilo:\n{body.context.strip()}")
    parts.append("\nRedacta el escrito completo ahora.")
    return "\n".join(parts)


@router.post("/generate")
async def generate_writ(
    body: GenerateRequest,
    principal: Principal = Depends(get_current_firm),
):
    guide = _DOC_GUIDES.get(body.doc_type, _DOC_GUIDES["otro"])
    system_prompt = f"{_SYSTEM_BASE}\n\n{guide}"
    user_prompt = _build_user_prompt(body)

    async def _stream() -> AsyncIterator[bytes]:
        client = get_openai_client()
        # Buffer to detect cross-chunk citations (regex on a sliding window).
        seen_citations: set[str] = set()
        carry = ""
        total_chars = 0

        try:
            yield (json.dumps({"type": "start", "doc_type": body.doc_type}) + "\n").encode()
            stream = await client.chat.completions.create(
                model="gpt-4o",
                temperature=0.3,
                max_tokens=4_000,
                stream=True,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            async for chunk in stream:
                delta = (chunk.choices[0].delta.content or "") if chunk.choices else ""
                if not delta:
                    continue
                total_chars += len(delta)
                yield (json.dumps({"type": "text", "delta": delta}) + "\n").encode()

                # Sliding-window citation detection.
                window = (carry + delta)[-400:]
                for m in _CITATION_RE.finditer(window):
                    ref = re.sub(r"\s+", " ", m.group(0)).strip()
                    if ref in seen_citations:
                        continue
                    seen_citations.add(ref)
                    yield (json.dumps({"type": "citation_detected", "ref": ref}) + "\n").encode()
                carry = window[-200:]

            yield (json.dumps({
                "type": "done",
                "total_chars": total_chars,
                "citations": list(seen_citations),
            }) + "\n").encode()
        except Exception as e:
            logger.error("canvas/generate failed: %s", e)
            yield (json.dumps({"type": "error", "message": str(e)[:240]}) + "\n").encode()

    return StreamingResponse(
        _stream(),
        media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no"},  # nginx · disable proxy buffering
    )
