"""Stage 2: Data Extractor — extrae datos del caso del brief.

Devuelve un dict con campos extraídos + lista de campos faltantes.
En M1 usa gpt-4o con JSON output genérico; M2 introducirá Pydantic schemas
específicos por TemplateDef.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ExtractionResult:
    extracted_fields: dict[str, Any] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)


# Schemas mínimos por doc_type (M2 expandirá con Pydantic completo)
DEFAULT_SCHEMAS: dict[str, dict[str, str]] = {
    "demanda_laboral_ordinaria": {
        "demandante_nombre": "str",
        "demandante_cc": "str",
        "demandada_razon_social": "str",
        "demandada_nit": "str",
        "salario_mensual": "number",
        "fecha_ingreso": "date YYYY-MM-DD",
        "fecha_despido": "date YYYY-MM-DD",
        "cargo": "str",
        "ciudad": "str",
    },
    "demanda_civil_ordinaria": {
        "demandante_nombre": "str",
        "demandado_nombre": "str",
        "pretension_principal": "str",
        "monto_reclamado": "number",
        "fecha_hechos": "date YYYY-MM-DD",
    },
    "tutela": {
        "accionante_nombre": "str",
        "accionante_cc": "str",
        "accionado_entidad": "str",
        "derecho_vulnerado": "str",
        "fecha_hecho": "date YYYY-MM-DD",
    },
    "derecho_peticion": {
        "peticionario_nombre": "str",
        "entidad_destinataria": "str",
        "peticion_concreta": "str",
    },
    "contrato_arrendamiento": {
        "arrendador_nombre": "str",
        "arrendatario_nombre": "str",
        "inmueble_direccion": "str",
        "canon_mensual": "number",
        "duracion_meses": "number",
    },
    "denuncia_penal": {
        "denunciante_nombre": "str",
        "denunciado_nombre": "str",
        "delito": "str",
        "fecha_hecho": "date YYYY-MM-DD",
    },
}


async def extract(
    client,
    doc_type: str,
    intent: str,
    brief: str | None = None,
) -> ExtractionResult:
    """Extrae datos estructurados del intent + brief."""
    schema = DEFAULT_SCHEMAS.get(doc_type, {
        "asunto": "str",
        "partes": "str",
        "fecha": "date YYYY-MM-DD",
    })

    schema_lines = "\n".join(f'  "{k}": "{v}"' for k, v in schema.items())

    system_prompt = (
        "Extrae los siguientes campos del INTENT y BRIEF que recibirás. "
        "Si un campo NO está presente, omítelo del JSON (no inventes). "
        "Responde SIEMPRE en JSON estricto, sin texto adicional."
    )
    user_prompt = f"""DOC TYPE: {doc_type}

CAMPOS A EXTRAER:
{{
{schema_lines}
}}

INTENT:
{intent}

BRIEF:
{brief or '(sin brief)'}

Responde con JSON estricto. Solo incluye campos efectivamente presentes.
"""

    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
            max_tokens=600,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        extracted = json.loads(raw)
    except Exception as e:
        logger.warning("extractor failed, returning empty: %s", e)
        extracted = {}

    # Calcular faltantes
    missing = [k for k in schema.keys() if k not in extracted or extracted.get(k) in (None, "", [])]

    return ExtractionResult(
        extracted_fields=extracted,
        missing_fields=missing,
    )
