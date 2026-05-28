"""M19.23.C — Data Completeness Gate Stage.

Patrón inspirado en el `doc-coauthoring` skill de Claude (Stage 1: Context
Gathering). Detecta si faltan datos críticos en el prompt del usuario
ANTES de redactar y, opcionalmente, pausa la generación para pedírselos.

Cuándo se ejecuta:
  - Después de `structure_discovery` y `data_extractor`.
  - Antes de `hunters_stage` / `block_generator`.

Comportamiento según modo:
  - **Modo "firma"** (`borrador_mode=False` o `strict_mode=True`):
    Si faltan datos críticos → emite SSE `missing_data` y pausa.
    El orchestrator espera la respuesta del usuario vía endpoint
    `/documents/v2/resume-generation` antes de continuar.
  - **Modo "borrador"** (`borrador_mode=True`, default):
    Continúa siempre, usa placeholders `[CEDULA_X]` para campos faltantes.
    Sólo emite `missing_data` informativo (no bloqueante).

Reusa al 100%:
  - Mismo OpenAI client del pipeline (gpt-4o-mini)
  - data_extractor output existente (no se modifica)
  - Schema de StructureRecipe de M19.23.B

Costo: ~$0.001-0.002 por documento (1 LLM call gpt-4o-mini).
Latencia: 3-8s.

Filosofía: el agente PREGUNTA cuando hay duda crítica, no asume.
Pero respeta la elección del usuario de continuar con placeholders
(modo borrador) o no (modo firma).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ============================================================
# Schema
# ============================================================

@dataclass
class MissingField:
    """Un campo de datos identificado como faltante."""
    field_key: str                   # 'cedula_demandante', 'fecha_matrimonio', etc.
    label: str                       # 'Cédula del demandante'
    description: str                 # explicación corta para el usuario
    severity: str = "critical"       # 'critical' (sin esto no se puede redactar) | 'optional' (placeholder acceptable)
    suggested_placeholder: Optional[str] = None  # ej. '[CEDULA_DEMANDANTE]'
    example_value: Optional[str] = None          # ej. '52.847.392 de Bogotá'


@dataclass
class DataCompletenessReport:
    """Output del stage. Se serializa a JSON para SSE event."""
    doc_type: str
    required_fields_count: int = 0
    extracted_fields_count: int = 0
    missing_critical: list[MissingField] = field(default_factory=list)
    missing_optional: list[MissingField] = field(default_factory=list)
    can_continue: bool = True             # False solo en modo strict con critical faltantes
    borrador_mode: bool = True            # modo en el que se ejecutó
    duration_ms: int = 0
    skipped: bool = False
    reasoning: str = ""                   # narrativa breve

    @property
    def has_missing(self) -> bool:
        return len(self.missing_critical) > 0 or len(self.missing_optional) > 0

    @property
    def missing_summary_for_user(self) -> str:
        """Mensaje natural para el usuario (estilo Claude)."""
        if not self.has_missing:
            return ""
        lines: list[str] = []
        if self.missing_critical:
            lines.append("**Datos necesarios para redactar el documento:**")
            for i, f in enumerate(self.missing_critical, 1):
                ex = f" (ej: {f.example_value})" if f.example_value else ""
                lines.append(f"{i}. **{f.label}** — {f.description}{ex}")
        if self.missing_optional:
            if lines:
                lines.append("")
            lines.append("**Datos opcionales (usaré placeholders si los omites):**")
            for j, f in enumerate(self.missing_optional, 1):
                lines.append(f"{j}. {f.label} — {f.description}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "doc_type": self.doc_type,
            "required_fields_count": self.required_fields_count,
            "extracted_fields_count": self.extracted_fields_count,
            "missing_critical": [asdict(f) for f in self.missing_critical],
            "missing_optional": [asdict(f) for f in self.missing_optional],
            "can_continue": self.can_continue,
            "borrador_mode": self.borrador_mode,
            "skipped": self.skipped,
            "reasoning": self.reasoning,
            "missing_summary": self.missing_summary_for_user,
            "duration_ms": self.duration_ms,
        }


EMPTY_REPORT = DataCompletenessReport(
    doc_type="unknown",
    skipped=True,
    can_continue=True,
    reasoning="data_completeness_gate skipped (no client, no recipe, or disabled)",
)


# ============================================================
# LLM prompt
# ============================================================

DATA_COMPLETENESS_PROMPT = """Eres un ABOGADO LITIGANTE SENIOR colombiano. Tu tarea es revisar el
prompt del usuario y determinar si tiene los datos mínimos NECESARIOS
para redactar un documento legal específico, basado en el doc_type y la
norma procesal aplicable.

Te pasan:
  1. doc_type del documento a generar
  2. norma_procesal aplicable (Art. 82 CGP, Art. 162 CPACA, etc.)
  3. juez_competente sugerido
  4. El prompt completo del usuario
  5. (Opcional) Los datos que ya se extrajeron del prompt

Tu output: lista de campos REQUERIDOS por la norma procesal, separados
en críticos y opcionales:

**CRÍTICOS** (sin estos NO se puede redactar legalmente):
  - Para CUALQUIER demanda: identificación completa partes (nombres + cédula/NIT)
  - Para CUALQUIER demanda: hechos concretos (al menos 1-2)
  - Para CUALQUIER demanda: pretensiones (al menos 1)
  - Específico por doc_type:
    * demanda_divorcio: fecha y lugar matrimonio, causales invocadas (Art. 154 CC)
    * demanda_pertenencia: identificación inmueble (dirección + matrícula),
      fecha inicio posesión, naturaleza posesión
    * demanda_laboral: vínculo laboral (fechas, salario, cargo)
    * demanda_nulidad_restablecimiento: identificación acto admin
      (resolución número + fecha), fecha agotamiento vía gubernativa
    * tutela: derecho fundamental vulnerado (Art. 11/12/13/etc. CN),
      acción/omisión de autoridad
    * demanda_responsabilidad_civil: descripción del daño, nexo causal
    * demanda_alimentos: edad del menor, capacidad económica alimentante

**OPCIONALES** (placeholders aceptables si faltan):
  - Direcciones físicas
  - Emails / teléfonos
  - Datos del apoderado
  - Detalles técnicos secundarios

REGLAS:
- NO inventes campos. Solo los exigidos por norma procesal del área.
- Si el campo se MENCIONA en el prompt → NO está faltante.
- Si el prompt usa placeholders explícitos (e.g., "[NOMBRE_DEMANDADO]") →
  marca el campo como CRÍTICO faltante.
- suggested_placeholder: usa formato [CAMPO_DESCRIPTIVO_MAYUS].
- example_value: da un ejemplo realista colombiano.

OUTPUT (JSON estricto sin markdown):
{
  "required_fields": ["lista de field_keys totales esperados"],
  "missing_critical": [
    {
      "field_key": "fecha_matrimonio",
      "label": "Fecha y lugar del matrimonio",
      "description": "Necesaria para acreditar el vínculo y aplicar Art. 388 CGP",
      "suggested_placeholder": "[FECHA_MATRIMONIO]",
      "example_value": "28 de junio de 2010 ante Notario 38 de Bogotá"
    }
  ],
  "missing_optional": [
    {
      "field_key": "telefono_demandante",
      "label": "Teléfono del demandante",
      "description": "Para notificaciones",
      "suggested_placeholder": "[TELEFONO]",
      "example_value": "3001234567"
    }
  ],
  "reasoning": "El prompt incluye nombres y causal pero falta la fecha del matrimonio."
}
"""


async def _llm_detect_missing(
    client,
    doc_type: str,
    norma_procesal_ref: Optional[str],
    juez_competente: Optional[str],
    intent: str,
    brief: Optional[str],
    extracted_data: dict,
) -> Optional[dict]:
    """LLM detecta datos faltantes. None si falla."""
    if client is None:
        return None
    try:
        extracted_summary = json.dumps(extracted_data, ensure_ascii=False, default=str)[:1500] if extracted_data else "(sin datos extraídos)"
        user_msg = f"""DOC TYPE: {doc_type}
NORMA PROCESAL APLICABLE: {norma_procesal_ref or '(no especificada)'}
JUEZ COMPETENTE: {juez_competente or '(no especificado)'}

PROMPT DEL USUARIO:
\"\"\"
{intent[:3000]}
\"\"\"

BRIEF ADICIONAL:
{(brief or '')[:1000]}

DATOS YA EXTRAÍDOS DEL PROMPT (por data_extractor):
{extracted_summary}

Identifica required_fields (lista total esperada según la norma procesal),
missing_critical (sin los cuales NO se puede redactar legalmente) y
missing_optional (placeholders aceptables). Devuelve JSON estricto."""

        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": DATA_COMPLETENESS_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.1,
            max_tokens=1500,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        return json.loads(raw)
    except Exception as e:
        logger.warning("data_completeness LLM call failed: %s", e)
        return None


# ============================================================
# Public entry point
# ============================================================

async def check_data_completeness(
    client,
    doc_type: str,
    intent: str,
    brief: Optional[str] = None,
    extracted_data: Optional[dict] = None,
    norma_procesal_ref: Optional[str] = None,
    juez_competente: Optional[str] = None,
    borrador_mode: bool = True,
    timeout_seconds: float = 25.0,
) -> DataCompletenessReport:
    """Stage principal.

    Args:
        borrador_mode: True (default) → no bloquea, solo informa.
                       False (modo firma) → bloquea si missing_critical > 0.

    Returns: DataCompletenessReport. Si falla, retorna EMPTY_REPORT
    (can_continue=True, skipped=True) para no romper el pipeline.
    """
    started = time.time()
    try:
        result = await asyncio.wait_for(
            _check_inner(
                client, doc_type, intent, brief, extracted_data,
                norma_procesal_ref, juez_competente, borrador_mode,
            ),
            timeout=timeout_seconds,
        )
        result.duration_ms = int((time.time() - started) * 1000)
        return result
    except asyncio.TimeoutError:
        logger.warning(
            "data_completeness_gate TIMEOUT after %.1fs — continuing without check",
            timeout_seconds,
        )
        r = EMPTY_REPORT
        r.doc_type = doc_type
        r.borrador_mode = borrador_mode
        r.reasoning = f"timeout after {timeout_seconds}s, pipeline continues"
        r.duration_ms = int((time.time() - started) * 1000)
        return r
    except Exception as e:
        logger.warning("data_completeness_gate exception (non-fatal): %s", e)
        r = EMPTY_REPORT
        r.doc_type = doc_type
        r.borrador_mode = borrador_mode
        r.reasoning = f"exception: {str(e)[:120]}"
        r.duration_ms = int((time.time() - started) * 1000)
        return r


async def _check_inner(
    client,
    doc_type: str,
    intent: str,
    brief: Optional[str],
    extracted_data: Optional[dict],
    norma_procesal_ref: Optional[str],
    juez_competente: Optional[str],
    borrador_mode: bool,
) -> DataCompletenessReport:
    """Implementación interna envuelta con timeout."""

    data = await _llm_detect_missing(
        client, doc_type, norma_procesal_ref, juez_competente,
        intent, brief, extracted_data or {},
    )

    if data is None:
        # LLM falló, asumir que hay suficientes datos para no bloquear
        return DataCompletenessReport(
            doc_type=doc_type,
            borrador_mode=borrador_mode,
            skipped=True,
            can_continue=True,
            reasoning="LLM unavailable, assuming sufficient data",
        )

    # Parse missing_critical
    missing_critical: list[MissingField] = []
    for f in (data.get("missing_critical") or [])[:10]:
        if not isinstance(f, dict):
            continue
        missing_critical.append(MissingField(
            field_key=str(f.get("field_key", "unknown"))[:60],
            label=str(f.get("label", ""))[:120],
            description=str(f.get("description", ""))[:300],
            severity="critical",
            suggested_placeholder=str(f.get("suggested_placeholder", ""))[:60] or None,
            example_value=str(f.get("example_value", ""))[:120] or None,
        ))

    # Parse missing_optional
    missing_optional: list[MissingField] = []
    for f in (data.get("missing_optional") or [])[:10]:
        if not isinstance(f, dict):
            continue
        missing_optional.append(MissingField(
            field_key=str(f.get("field_key", "unknown"))[:60],
            label=str(f.get("label", ""))[:120],
            description=str(f.get("description", ""))[:300],
            severity="optional",
            suggested_placeholder=str(f.get("suggested_placeholder", ""))[:60] or None,
            example_value=str(f.get("example_value", ""))[:120] or None,
        ))

    required_count = len(data.get("required_fields") or [])
    extracted_count = len(extracted_data) if extracted_data else 0

    # Gate decision
    # En modo borrador NUNCA bloquea (continúa con placeholders).
    # En modo firma bloquea solo si hay críticos faltantes.
    can_continue = borrador_mode or (len(missing_critical) == 0)

    report = DataCompletenessReport(
        doc_type=doc_type,
        required_fields_count=required_count,
        extracted_fields_count=extracted_count,
        missing_critical=missing_critical,
        missing_optional=missing_optional,
        can_continue=can_continue,
        borrador_mode=borrador_mode,
        reasoning=str(data.get("reasoning", ""))[:400],
    )

    logger.info(
        "data_completeness_gate: doc_type=%s missing_critical=%d missing_optional=%d borrador=%s can_continue=%s",
        doc_type, len(missing_critical), len(missing_optional),
        borrador_mode, can_continue,
    )
    return report
