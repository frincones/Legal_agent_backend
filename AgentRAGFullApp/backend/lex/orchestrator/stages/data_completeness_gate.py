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

DATA_COMPLETENESS_PROMPT = """Eres un ABOGADO LITIGANTE SENIOR colombiano con 20+ años de experiencia
en redacción de documentos LISTOS-PARA-FIRMA. Tu trabajo es analizar el
prompt del usuario y producir el SCHEMA COMPLETO Y EXHAUSTIVO de datos
que se requieren para redactar el documento legal especificado, sin
asumir conocimiento previo del LLM sobre el caso concreto.

═══════════════════════════════════════════════════════════════
METODOLOGÍA OBLIGATORIA (no la saltes — cada paso es necesario)
═══════════════════════════════════════════════════════════════

PASO 1 — IMAGINAR EL DOCUMENTO TERMINADO
Visualiza mentalmente el documento legal completo, sección por sección.
Usa el `sections_plan` que te paso (lo descubrió `structure_discovery`)
como guía. Si no hay plan, infiere las secciones obligatorias según la
norma procesal aplicable.

PASO 2 — INVENTARIO POR CAPAS (sé exhaustivo)
Recorre EXPLÍCITAMENTE cada capa de datos y enumera qué campos requiere
el documento. Mínimo debes considerar TODAS estas capas:

  A. DATOS DE LAS PARTES
     - Nombres completos (no parciales) de cada parte
     - Documentos de identidad (CC/NIT/CE/Pasaporte) con número y lugar
     - Domicilios reales (dirección, ciudad, departamento)
     - Estado civil, ocupación, representación legal si aplica
     - Para personas jurídicas: representante legal + NIT + matrícula

  B. DATOS DEL DOCUMENTO/HECHO GENERADOR
     - Fecha exacta del hecho/acto/contrato/relación
     - Lugar del hecho
     - Identificación documental (escritura, número, notaría, registro,
       matrícula inmobiliaria, placa vehículo, factura, pagaré, etc.)
     - Características del bien/relación (avalúo, área, especificaciones)

  C. DATOS SUSTANTIVOS DEL DERECHO
     - Causales/fundamentos invocados (con norma específica)
     - Hechos concretos (mínimo 3-5 hechos para una demanda real)
     - Pretensiones específicas (con monto si es de condena)

  D. DATOS ECONÓMICOS
     - Montos exactos con concepto (no solo "$X" sino "$X por concepto Y")
     - Fechas de los montos (mora desde cuándo, intereses, etc.)
     - Salario/ingresos si aplica (para alimentos, indemnizaciones)
     - Fórmulas (tasa interés, IPC, SMMLV, índices)
     - Cuantía estimada total

  E. DATOS PROCESALES
     - Juzgado competente (con jurisdicción y circuito específico)
     - Anexos/pruebas (lista detallada de documentos a aportar)
     - Apoderado (nombre, T.P., correo, cuenta para notificaciones)
     - Datos para notificación de cada parte (físico + electrónico)
     - Juramento (norma de respaldo)

  F. DATOS ACCESORIOS Y MENORES (si aplica)
     - Hijos: nombres COMPLETOS, edades, registros civiles, custodia actual
     - Bienes en sociedad: matrículas, placas, avalúos por separado
     - Medidas cautelares solicitadas con sustento
     - Pretensiones subsidiarias

  G. DATOS DE FECHAS RELEVANTES
     - Para procesal: agotamiento vía gubernativa, prescripción, caducidad
     - Para sustantivo: inicio posesión, mora, fecha pago, vencimiento

PASO 3 — CLASIFICAR SEVERIDAD POR CAMPO
Para CADA campo del inventario asigna severity:

  • CRITICAL → El documento NO puede firmarse ni presentarse sin este
    dato. Si queda como "[PLACEHOLDER]" el documento es defectuoso o
    inadmisible. Ejemplos universales:
      - Identificación COMPLETA de todas las partes (no solo nombre)
      - Fecha exacta del hecho generador del derecho/obligación
      - Datos del título ejecutivo si es proceso ejecutivo
      - Causal invocada si la norma exige una en específico
      - Para alimentos: ingresos del alimentante + datos del menor
      - Para divorcio: fecha exacta matrimonio + datos hijos si los hay
      - Para pertenencia: matrícula inmobiliaria + fecha inicio posesión
      - Para laboral: fechas vínculo + último salario
      - Para tutela: derecho fundamental específico + autoridad/particular

  • OPTIONAL → Mejora el documento pero se puede usar placeholder o
    deferir. Ejemplos: teléfono, email secundario, número juzgado
    específico de reparto, dirección si solo se conoce ciudad,
    profesión/ocupación si no es esencial al caso.

PASO 4 — MATCH CONTRA EL PROMPT
Para cada campo del inventario, busca si el usuario ya lo proporcionó
en su prompt (intent + brief + extracted_data):

  • SÍ está (aunque parcial) → NO va en missing
  • NO está → va en missing_critical o missing_optional según severity
  • El usuario dijo "lo confirmo después" o usó placeholder explícito
    (ej: "[FECHA_MATRIMONIO]", "X de X de 2024") → va en
    missing_critical (no como opcional)

PASO 5 — AUTO-CRÍTICA DE EXHAUSTIVIDAD
Antes de finalizar, revisa tu output:
  - ¿required_fields tiene al menos 12-25 entradas? (menos = perezoso)
  - ¿Cubrí TODAS las capas A-G arriba?
  - ¿Marqué campos compuestos como múltiples campos? (ej: "los hijos"
    no es 1 campo, son N hijos × {nombre, edad, registro_civil})
  - ¿Hay al menos 3-6 críticos faltantes? Para un prompt incompleto,
    1-2 críticos es señal de que NO fuiste exhaustivo.

═══════════════════════════════════════════════════════════════
REGLAS NO NEGOCIABLES
═══════════════════════════════════════════════════════════════

- NO asumas que el LLM/agente sabe los datos. Si NO están en el prompt,
  FALTAN — aunque el LLM "podría rellenar genéricos plausibles".
- field_key debe ser snake_case_descriptivo y único.
- Para datos compuestos genera múltiples entradas con sufijo numérico:
  hijo_1_nombre, hijo_1_edad, hijo_1_registro_civil, hijo_2_nombre, ...
- example_value: ejemplo realista COLOMBIANO concreto (no genérico
  "Juan Pérez" sino algo como "María Fernanda Gómez Restrepo,
  C.C. 43.215.789 de Medellín").
- suggested_placeholder: [CAMPO_DESCRIPTIVO_EN_MAYUS_CON_GUION_BAJO].

═══════════════════════════════════════════════════════════════
OUTPUT (SOLO JSON válido, sin markdown, sin comentarios)
═══════════════════════════════════════════════════════════════

{
  "reasoning_chain": "PASO 1: el documento es <doc_type>, tendrá secciones [...]. PASO 2: las capas A-G requieren campos [...]. PASO 3: críticos son [...] porque [...]. PASO 4: el usuario mencionó [...] pero faltan [...]. PASO 5: auto-crítica: required_fields tiene N entradas, cubrí todas las capas.",
  "doc_sections_imagined": ["lista de secciones del documento mental"],
  "required_fields": ["field_key_1", "field_key_2", "...", "field_key_N"],
  "missing_critical": [
    {
      "field_key": "fecha_matrimonio",
      "label": "Fecha y lugar exactos del matrimonio civil",
      "description": "Necesaria para acreditar el vínculo conyugal y aplicar Art. 154 CC (causales de divorcio) + Art. 388 CGP (competencia). Sin esto la demanda es inadmisible.",
      "suggested_placeholder": "[FECHA_MATRIMONIO]",
      "example_value": "28 de junio de 2010 ante Notario 38 de Bogotá, inscrito en Registro Civil 12345"
    }
  ],
  "missing_optional": [
    {
      "field_key": "telefono_demandante",
      "label": "Teléfono del demandante para notificaciones",
      "description": "Mejora notificación electrónica del Art. 291 CGP",
      "suggested_placeholder": "[TELEFONO_DEMANDANTE]",
      "example_value": "3001234567"
    }
  ],
  "reasoning": "Resumen breve y natural (3-4 líneas) para el usuario explicando qué falta y por qué importa para la firmabilidad del documento."
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
    sections_plan: Optional[list] = None,
    model: str = "gpt-4o",
) -> Optional[dict]:
    """LLM detecta datos faltantes. None si falla.

    M19.23.K — Usa gpt-4o (no mini) para razonamiento legal exhaustivo.
    Acepta sections_plan del structure_recipe para guiar la imaginación
    del documento terminado (mejora exhaustividad).
    """
    if client is None:
        return None
    try:
        extracted_summary = (
            json.dumps(extracted_data, ensure_ascii=False, default=str)[:2000]
            if extracted_data else "(sin datos extraídos)"
        )
        sections_block = ""
        if sections_plan:
            try:
                titles = []
                for s in sections_plan[:20]:
                    if isinstance(s, dict):
                        t = s.get("title") or s.get("key") or "?"
                        roman = s.get("roman") or ""
                        titles.append(f"  {roman}. {t}")
                if titles:
                    sections_block = "PLAN DE SECCIONES (structure_discovery):\n" + "\n".join(titles) + "\n\n"
            except Exception:
                sections_block = ""

        user_msg = f"""DOC TYPE: {doc_type}
NORMA PROCESAL APLICABLE: {norma_procesal_ref or '(no especificada)'}
JUEZ COMPETENTE: {juez_competente or '(no especificado)'}

{sections_block}PROMPT DEL USUARIO:
\"\"\"
{intent[:4000]}
\"\"\"

BRIEF ADICIONAL:
{(brief or '')[:1500]}

DATOS YA EXTRAÍDOS DEL PROMPT (por data_extractor):
{extracted_summary}

Aplica la METODOLOGÍA OBLIGATORIA (Pasos 1 a 5). Sé EXHAUSTIVO: lista
required_fields con 12-25 entradas mínimo cubriendo capas A a G. Llena
reasoning_chain con tu análisis explícito paso a paso. Devuelve JSON
estricto válido."""

        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": DATA_COMPLETENESS_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.1,
            max_tokens=4000,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        return json.loads(raw)
    except Exception as e:
        logger.warning("data_completeness LLM call failed (model=%s): %s", model, e)
        # Fallback a gpt-4o-mini si gpt-4o no está disponible/quota
        if model != "gpt-4o-mini":
            logger.info("data_completeness retry con gpt-4o-mini fallback")
            try:
                resp = await client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": DATA_COMPLETENESS_PROMPT},
                        {"role": "user", "content": user_msg},
                    ],
                    temperature=0.1,
                    max_tokens=3000,
                    response_format={"type": "json_object"},
                )
                raw = resp.choices[0].message.content or "{}"
                return json.loads(raw)
            except Exception as e2:
                logger.warning("data_completeness fallback gpt-4o-mini also failed: %s", e2)
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
    sections_plan: Optional[list] = None,
    borrador_mode: bool = True,
    timeout_seconds: float = 40.0,
    model: str = "gpt-4o",
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
                norma_procesal_ref, juez_competente, sections_plan,
                borrador_mode, model,
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
    sections_plan: Optional[list],
    borrador_mode: bool,
    model: str,
) -> DataCompletenessReport:
    """Implementación interna envuelta con timeout."""

    data = await _llm_detect_missing(
        client, doc_type, norma_procesal_ref, juez_competente,
        intent, brief, extracted_data or {},
        sections_plan=sections_plan, model=model,
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
