"""Slot filler · resolve template {{variables}} from CaseState + extractions.

Used by:
  - agent/workers/document_generator.py (Sprint 3 multi-agent)
  - api/multi_agent_generate.py endpoint (pre-flight slot resolution)

Sources (in priority order):
  1. user_overrides — explicit values passed in the request
  2. matter_documents.extractions — structured extractions per document
  3. matters row — titulo, materia, expediente, tribunal, etc.
  4. LLM inference (extract_structured) over the user_brief if still missing

The output is { filled: {slot: value}, missing: [slot, ...] }. Missing slots
are surfaced to the user so the multi-agent can ask clarifying questions or
mark them as "[CONFIRMAR]" placeholders in the draft.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from agent.llm_skills import ExtractionField, extract_structured
from utils.llm_tiers import Tier

logger = logging.getLogger(__name__)

_VARIABLE_RE = re.compile(r"\{\{\s*([a-zA-Z][a-zA-Z0-9_.]*)\s*\}\}")


# Conventional names · short descriptions used to coach the LLM extractor.
SLOT_HINTS: dict[str, str] = {
    "ciudad": "Ciudad donde se presenta el documento (ej. Bogotá D.C.)",
    "nombre_demandante": "Nombre completo del demandante / accionante",
    "cc_demandante": "Cédula de ciudadanía del demandante (solo dígitos)",
    "nombre_demandado": "Nombre completo del demandado / accionado",
    "id_demandado": "NIT o cédula del demandado",
    "nit_demandado": "NIT del demandado (solo dígitos)",
    "direccion_demandante": "Dirección física del demandante",
    "direccion_demandado": "Dirección física del demandado",
    "correo_demandante": "Correo electrónico del demandante",
    "correo_demandado": "Correo electrónico del demandado",
    "tipo_contrato": "Modalidad del contrato (indefinido / término fijo / obra)",
    "fecha_ingreso": "Fecha de inicio de la relación / contrato (YYYY-MM-DD)",
    "fecha_terminacion": "Fecha de terminación o evento dañoso (YYYY-MM-DD)",
    "cargo": "Cargo o función desempeñada",
    "salario_mensual_cop": "Salario mensual en pesos colombianos",
    "cuantia_cop": "Cuantía estimada del proceso en pesos colombianos",
    "cuantia_smmlv": "Cuantía expresada en SMMLV (número con un decimal)",
    "anio_smmlv": "Año del SMMLV de referencia",
    "valor_obligacion_cop": "Valor de la obligación en pesos colombianos",
    "fecha_vencimiento": "Fecha de vencimiento (YYYY-MM-DD)",
    "fecha_titulo": "Fecha del título ejecutivo (YYYY-MM-DD)",
    "descripcion_titulo": "Descripción del título ejecutivo (pagaré, factura, etc.)",
    "diagnostico": "Diagnóstico médico relevante (texto libre)",
    "prestacion_ordenada": "Prestación o tratamiento ordenado",
    "fecha_orden_medica": "Fecha de la orden médica (YYYY-MM-DD)",
    "fecha_diagnostico": "Fecha del diagnóstico (YYYY-MM-DD)",
    "fecha_afiliacion": "Fecha de afiliación al sistema (YYYY-MM-DD)",
    "regimen": "Régimen de afiliación (contributivo / subsidiado)",
    "nombre_accionante": "Nombre completo del accionante (tutela)",
    "cc_accionante": "Cédula del accionante",
    "nombre_accionado": "Nombre del accionado (EPS, entidad, etc.)",
    "nit_accionado": "NIT del accionado",
    "correo_accionante": "Correo del accionante",
    "direccion_accionante": "Dirección del accionante",
    "telefono_accionante": "Teléfono del accionante",
    "correo_accionado": "Correo del accionado",
    "nombre_apoderado": "Nombre del apoderado judicial",
    "cc_apoderado": "Cédula del apoderado",
    "tp_apoderado": "Tarjeta profesional del apoderado",
    "correo_apoderado": "Correo del apoderado",
    "fecha_contrato": "Fecha de suscripción del contrato (YYYY-MM-DD)",
    "fecha_actual": "Fecha actual (YYYY-MM-DD)",
    "fecha_acto": "Fecha del acto administrativo (YYYY-MM-DD)",
    "fecha_notificacion": "Fecha de notificación (YYYY-MM-DD)",
    "acto_recurrido": "Identificador del acto administrativo recurrido",
    "descripcion_acto": "Descripción del acto administrativo",
    "fundamentos_recurso": "Fundamentos jurídicos del recurso (texto libre)",
    "modificacion_subsidiaria": "Pretensión subsidiaria de modificación",
    "nombre_recurrente": "Nombre del recurrente",
    "cc_recurrente": "Cédula del recurrente",
    "direccion_recurrente": "Dirección de notificación",
    "correo_recurrente": "Correo del recurrente",
    "cargo_funcionario": "Cargo del funcionario destinatario",
    "entidad": "Entidad destinataria",
    "ciudad_destinatario": "Ciudad de la entidad destinataria",
    "cargo_destinatario": "Cargo del destinatario",
    "entidad_destinatario": "Entidad destinataria",
    "informacion_solicitada_1": "Primer dato solicitado",
    "informacion_solicitada_2": "Segundo dato solicitado",
    "informacion_solicitada_3": "Tercer dato solicitado",
    "direccion_peticionario": "Dirección del peticionario",
    "correo_peticionario": "Correo del peticionario",
    "nombre_peticionario": "Nombre del peticionario",
    "cc_peticionario": "Cédula del peticionario",
    "representante_legal": "Nombre del representante legal",
    "representante_legal_empleador": "Nombre del representante legal del empleador",
    "forma_terminacion": "Forma en que se dio la terminación (carta, verbal, etc.)",
    "hechos_adicionales": "Otros hechos relevantes (texto libre)",
    "fechas_solicitudes": "Fechas en que se presentaron solicitudes previas",
    "otros_derechos": "Otros derechos fundamentales invocados",
}


@dataclass
class SlotFillResult:
    filled: dict[str, Any]
    missing: list[str]
    sources: dict[str, str]            # slot → source key (matter/extraction/inferred/override)
    confidence: dict[str, float]       # slot → 0-1 (1.0 for hardcoded sources)


def extract_variables_from_template(content_md: str) -> list[str]:
    """Return the sorted unique set of {{variable}} names in a template."""
    return sorted(set(_VARIABLE_RE.findall(content_md or "")))


async def fill_slots(
    *,
    template_content: str,
    user_brief: str,
    overrides: Optional[dict[str, Any]] = None,
    matter_row: Optional[dict[str, Any]] = None,
    extractions: Optional[list[dict[str, Any]]] = None,
    infer_missing: bool = True,
    session_id: str = "",
) -> SlotFillResult:
    """Resolve template variables from available sources.

    Args:
        template_content: The template_md with {{variables}}.
        user_brief: Natural language brief / message from the user.
        overrides: Explicit slot values provided by the caller (highest priority).
        matter_row: Optional dict from `matters` table.
        extractions: List of dicts from `document_extractions` (matter docs).
        infer_missing: If True, run an LLM extractor over user_brief for slots
                       that remain unresolved after the deterministic passes.
    """
    slots = extract_variables_from_template(template_content)
    if not slots:
        return SlotFillResult(filled={}, missing=[], sources={}, confidence={})

    filled: dict[str, Any] = {}
    sources: dict[str, str] = {}
    confidence: dict[str, float] = {}

    # 1. Overrides (caller-provided).
    if overrides:
        for k, v in overrides.items():
            if k in slots and v not in (None, ""):
                filled[k] = v
                sources[k] = "override"
                confidence[k] = 1.0

    # 2. Matter row · simple mapping.
    if matter_row:
        matter_map: dict[str, str] = {
            "ciudad": matter_row.get("ciudad") or "",
            "expediente": matter_row.get("expediente") or "",
        }
        # Materia-specific party name aliases.
        if matter_row.get("titulo"):
            matter_map["matter_titulo"] = matter_row["titulo"]
        for k, v in matter_map.items():
            if k in slots and k not in filled and v:
                filled[k] = v
                sources[k] = "matter"
                confidence[k] = 0.9

    # 3. Extractions · scan each document's parties/dates/amounts.
    if extractions:
        for ex in extractions:
            for slot_name in slots:
                if slot_name in filled:
                    continue
                v = _scan_extraction(ex, slot_name)
                if v:
                    filled[slot_name] = v
                    sources[slot_name] = "extraction"
                    confidence[slot_name] = 0.75

    # 4. LLM inference on user_brief for remaining slots.
    missing = [s for s in slots if s not in filled]
    if infer_missing and missing and user_brief.strip():
        fields = [
            ExtractionField(
                name=s,
                description=SLOT_HINTS.get(s, f"Valor para {s} (deducir del brief)"),
                required=False,
            )
            for s in missing
        ]
        try:
            result = await extract_structured(
                text=user_brief,
                fields=fields,
                instructions=(
                    "Devuelve null para cualquier campo que no aparezca o no se "
                    "pueda deducir con alta confianza · NO inventes datos."
                ),
                tier=Tier.WORKER,
                purpose="slot_filler:infer",
                session_id=session_id,
            )
            for s in missing:
                v = result.data.get(s)
                if v not in (None, "", "null"):
                    filled[s] = v
                    sources[s] = "inferred"
                    confidence[s] = 0.55
        except Exception as e:
            logger.warning("slot_filler LLM inference failed: %s", e)

    final_missing = [s for s in slots if s not in filled]
    return SlotFillResult(
        filled=filled,
        missing=final_missing,
        sources=sources,
        confidence=confidence,
    )


def render_template(content_md: str, slots: dict[str, Any]) -> str:
    """Substitute {{var}} → value · unknown vars stay as `[CONFIRMAR: var]`."""

    def repl(m: re.Match) -> str:
        name = m.group(1)
        v = slots.get(name)
        if v in (None, ""):
            return f"[CONFIRMAR: {name}]"
        return str(v)

    return _VARIABLE_RE.sub(repl, content_md)


# ──────────────────────────────────────────────────────────────
# Internals
# ──────────────────────────────────────────────────────────────


# Simple alias map so a slot like 'nombre_demandante' can pick up
# 'demandante' or 'accionante' fields from extractions, etc.
_EXTRACTION_ALIASES: dict[str, list[str]] = {
    "nombre_demandante": ["demandante", "accionante", "actor", "querellante"],
    "nombre_demandado": ["demandado", "accionado", "querellado"],
    "cc_demandante": ["cc_demandante", "cedula_demandante", "id_demandante"],
    "nit_demandado": ["nit_demandado", "nit_accionado", "rut_demandado"],
    "fecha_ingreso": ["fecha_ingreso", "fecha_inicio_contrato"],
    "fecha_terminacion": ["fecha_terminacion", "fecha_fin_contrato", "fecha_despido"],
    "salario_mensual_cop": ["salario_mensual", "salario", "remuneracion"],
    "diagnostico": ["diagnostico", "patologia", "enfermedad"],
}


def _scan_extraction(ex: dict[str, Any], slot_name: str) -> Optional[str]:
    """Try to find a value for slot_name in one extraction record."""
    candidates: list[str] = [slot_name] + _EXTRACTION_ALIASES.get(slot_name, [])

    # Top-level keys.
    for k in candidates:
        v = ex.get(k)
        if v not in (None, "", []):
            return _stringify(v)

    # Common nested buckets in document_extractions.
    for bucket in ("parties_jsonb", "dates_jsonb", "obligations_jsonb", "montos_jsonb"):
        b = ex.get(bucket)
        if not isinstance(b, dict):
            continue
        for k in candidates:
            v = b.get(k)
            if v not in (None, "", []):
                return _stringify(v)

    return None


def _stringify(v: Any) -> str:
    if isinstance(v, (str, int, float)):
        return str(v)
    if isinstance(v, list) and v:
        return ", ".join(_stringify(x) for x in v)
    if isinstance(v, dict):
        for k in ("nombre", "name", "value", "text"):
            if k in v:
                return _stringify(v[k])
    return str(v)
