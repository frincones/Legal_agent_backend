"""M19.20.A — Template Contracts: definición declarativa por doc_type de QUÉ
debe contener un documento legal colombiano de calidad firmable.

Esta tabla es la "fuente de verdad" para los stages completeness_check (M19.20.B)
y coherence_check (M19.20.C). Antes de marcar `ready_for_signature=true`, el
QualityReport (M19.20.D) verifica que cada contrato se cumpla.

Filosofía:
  - Un contrato describe la estructura MÍNIMA legal: secciones obligatorias,
    bloques mínimos por sección, normas autoritativas (juramento, jurisdicción).
  - El LLM puede agregar MÁS bloques o secciones; pero menos = doc incompleto.
  - Si el doc_type NO está mapeado, se usa DEFAULT_CONTRACT (laxo, solo
    valida presencia mínima genérica).

Cada contrato es un dict puro JSON-serializable para facilitar testing y
versionado. NO mezclar lógica aquí (solo declarativo).
"""
from __future__ import annotations

from typing import TypedDict, NotRequired


class BlockRule(TypedDict, total=False):
    """Regla mínima de bloques dentro de una sección."""
    min: int                              # cantidad mínima
    type: NotRequired[str]                # tipo esperado (hecho, pretension, list_item, etc.)
    must_contain_subsections: NotRequired[list[str]]  # ej. ["7.1", "7.2", "7.3"]
    blocks_required: NotRequired[list[str]]  # tipos que DEBEN aparecer (norma_citada, jurisprudencia)


class TemplateContract(TypedDict, total=False):
    doc_type: str
    area: str                             # 'civil', 'laboral', 'familia', 'admin', 'penal', 'constitucional'
    descripcion: str                      # human-readable
    secciones_obligatorias: list[str]     # section_keys ordenadas
    secciones_no_aplican: list[str]       # secciones que NO deben aparecer (ej. juramento en tutela)
    min_bloques_por_seccion: dict[str, BlockRule]
    juramento_norma_ref: str              # "" si no aplica
    competencia_juez_default: str         # ej. "Juez Civil del Circuito"
    cuerpos_normativos_minimos: list[str] # ej. ["CGP", "CC"] — debe citarse al menos uno de cada
    pretensiones_verbos_validos: list[str] # ej. ["DECLARAR", "CONDENAR", "ORDENAR"]
    requires_liquidacion: bool            # si true, debe haber sección/tabla de liquidación
    requires_juramento: bool              # default true; false para tutela
    requires_firma: bool                  # default true
    # M19.20.B notas extra para LLM substantive check
    notas_calidad: list[str]              # checklist específico que el LLM debe revisar


# ============================================================
# DEFAULT CONTRACT (cuando doc_type no está mapeado)
# ============================================================

DEFAULT_CONTRACT: TemplateContract = {
    "doc_type": "default",
    "area": "general",
    "descripcion": "Contrato laxo: solo valida presencia mínima de secciones genéricas",
    "secciones_obligatorias": ["partes", "hechos", "pretensiones", "firma"],
    "secciones_no_aplican": [],
    "min_bloques_por_seccion": {
        "hechos": {"min": 3},
        "pretensiones": {"min": 2},
    },
    "juramento_norma_ref": "",
    "competencia_juez_default": "Juez competente",
    "cuerpos_normativos_minimos": [],
    "pretensiones_verbos_validos": ["DECLARAR", "CONDENAR", "ORDENAR", "SOLICITAR"],
    "requires_liquidacion": False,
    "requires_juramento": False,
    "requires_firma": True,
    "notas_calidad": ["Documento debe estar coherente y completo."],
}


# ============================================================
# CIVIL
# ============================================================

_CIVIL_BASE: TemplateContract = {
    "area": "civil",
    "secciones_obligatorias": [
        "encabezado", "partes", "hechos", "pretensiones", "fundamentos",
        "competencia_cuantia", "pruebas", "anexos", "notificaciones",
        "juramento", "firma",
    ],
    "secciones_no_aplican": [],
    "min_bloques_por_seccion": {
        "hechos": {"min": 4, "type": "hecho"},
        "pretensiones": {"min": 2, "type": "pretension"},
        "fundamentos": {"min": 3, "blocks_required": ["norma_citada"]},
        "pruebas": {"min": 3, "type": "list_item"},
    },
    "juramento_norma_ref": "Art. 206 CGP (Ley 1564/2012)",
    "competencia_juez_default": "Juez Civil del Circuito",
    "cuerpos_normativos_minimos": ["CGP", "CC"],
    "pretensiones_verbos_validos": ["DECLARAR", "CONDENAR", "ORDENAR"],
    "requires_liquidacion": False,
    "requires_juramento": True,
    "requires_firma": True,
    "notas_calidad": [
        "Pretensiones deben usar verbos en MAYÚSCULAS y BOLD (DECLARAR, CONDENAR, ORDENAR).",
        "Cada pretensión debe tener sustento en al menos un hecho narrado.",
        "Cuantía declarada debe coincidir con la suma de pretensiones de condena.",
    ],
}


CIVIL_ORDINARIA: TemplateContract = {
    **_CIVIL_BASE,
    "doc_type": "demanda_civil_ordinaria",
    "descripcion": "Demanda ordinaria civil (procesos declarativos generales)",
}

PERTENENCIA: TemplateContract = {
    **_CIVIL_BASE,
    "doc_type": "demanda_pertenencia",
    "descripcion": "Acción de pertenencia (prescripción adquisitiva)",
    "secciones_obligatorias": [
        "encabezado", "partes", "hechos", "pretensiones", "fundamentos",
        "competencia_cuantia", "pruebas", "anexos", "notificaciones",
        "juramento", "firma",
    ],
    "min_bloques_por_seccion": {
        **_CIVIL_BASE["min_bloques_por_seccion"],
        "hechos": {"min": 5, "type": "hecho"},  # debe narrar posesión: inicio, actos, tiempo, naturaleza, etc.
    },
    "notas_calidad": [
        "Debe demostrar posesión MATERIAL, PACÍFICA, PÚBLICA e ININTERRUMPIDA.",
        "El tiempo de posesión declarado debe ser ≥10 años (ordinaria) o ≥20 años (extraordinaria, Art. 2532 CC).",
        "Linderos del inmueble deben estar mencionados.",
        "Matrícula inmobiliaria debe aparecer en hechos y pretensiones de forma consistente.",
        "Pretensión principal debe usar 'DECLARAR ... ha adquirido por PRESCRIPCIÓN ADQUISITIVA'.",
        "Pretensión de inscripción en ORIP debe estar presente.",
    ],
}

RESPONSABILIDAD_CIVIL: TemplateContract = {
    **_CIVIL_BASE,
    "doc_type": "demanda_responsabilidad_civil",
    "descripcion": "Demanda de responsabilidad civil extracontractual",
    "requires_liquidacion": True,
    "min_bloques_por_seccion": {
        **_CIVIL_BASE["min_bloques_por_seccion"],
        "hechos": {"min": 5, "type": "hecho"},
        "pretensiones": {"min": 4, "type": "pretension"},
    },
    "notas_calidad": [
        "Daño debe estar descrito (emergente, lucro cesante, moral, fisiológico).",
        "Nexo causal entre conducta y daño debe estar explícito en hechos.",
        "Liquidación de perjuicios debe estar con fórmulas y montos.",
        "Cuantía total debe coincidir con suma de daño emergente + lucro cesante + moral.",
    ],
}


# ============================================================
# FAMILIA
# ============================================================

_FAMILIA_BASE: TemplateContract = {
    **_CIVIL_BASE,
    "area": "familia",
    "competencia_juez_default": "Juez de Familia del Circuito",
    "cuerpos_normativos_minimos": ["CGP", "CC", "Ley 1098/2006"],
}

DIVORCIO: TemplateContract = {
    **_FAMILIA_BASE,
    "doc_type": "demanda_divorcio",
    "descripcion": "Demanda de divorcio contencioso",
    "notas_calidad": [
        "Debe identificar la causal del Art. 154 CC.",
        "Hechos deben narrar el matrimonio y la causal alegada.",
    ],
}

ALIMENTOS: TemplateContract = {
    **_FAMILIA_BASE,
    "doc_type": "demanda_alimentos",
    "descripcion": "Demanda de alimentos",
    "requires_liquidacion": True,
    "notas_calidad": [
        "Edad del menor/alimentario debe estar mencionada.",
        "Capacidad económica del alimentante debe estar acreditada.",
        "Monto solicitado debe estar fundamentado.",
    ],
}


# ============================================================
# LABORAL
# ============================================================

LABORAL_ORDINARIA: TemplateContract = {
    "doc_type": "demanda_laboral_ordinaria",
    "area": "laboral",
    "descripcion": "Demanda ordinaria laboral (CST + CPTSS)",
    "secciones_obligatorias": [
        "encabezado", "partes", "hechos", "pretensiones", "fundamentos",
        "competencia_cuantia", "liquidacion", "pruebas", "anexos",
        "notificaciones", "juramento", "firma",
    ],
    "secciones_no_aplican": [],
    "min_bloques_por_seccion": {
        "hechos": {"min": 5, "type": "hecho"},
        "pretensiones": {"min": 3, "type": "pretension"},
        "fundamentos": {"min": 4, "blocks_required": ["norma_citada"]},
        "pruebas": {"min": 3, "type": "list_item"},
        "liquidacion": {"min": 1},  # debe haber al menos calc_step o table
    },
    "juramento_norma_ref": "Art. 28 CPTSS",
    "competencia_juez_default": "Juez Laboral del Circuito",
    "cuerpos_normativos_minimos": ["CST", "CPTSS"],
    "pretensiones_verbos_validos": ["DECLARAR", "CONDENAR", "ORDENAR"],
    "requires_liquidacion": True,
    "requires_juramento": True,
    "requires_firma": True,
    "notas_calidad": [
        "Salario mensual debe estar declarado y coincidir entre hechos y liquidación.",
        "Tiempo de servicio (fechas inicio/fin) debe estar explícito.",
        "Pretensiones deben separar declarativas (contrato realidad, ineficacia) y de condena (cesantías, primas, indemnización).",
        "Sanción moratoria (Art. 65 CST) debe estar pedida si hay impago.",
        "Cuantía declarada = suma de prestaciones + indemnizaciones.",
    ],
}


# ============================================================
# ADMINISTRATIVO
# ============================================================

NULIDAD_RESTABLECIMIENTO: TemplateContract = {
    "doc_type": "demanda_nulidad_restablecimiento",
    "area": "admin",
    "descripcion": "Demanda de nulidad y restablecimiento del derecho (CPACA)",
    "secciones_obligatorias": [
        "encabezado", "partes", "acto_administrativo", "hechos", "pretensiones",
        "fundamentos", "competencia_cuantia", "pruebas", "anexos",
        "notificaciones", "juramento", "firma",
    ],
    "secciones_no_aplican": [],
    "min_bloques_por_seccion": {
        "hechos": {"min": 4, "type": "hecho"},
        "pretensiones": {"min": 2, "type": "pretension"},
        "fundamentos": {"min": 3, "blocks_required": ["norma_citada"]},
    },
    "juramento_norma_ref": "Art. 167 CPACA (Ley 1437/2011)",
    "competencia_juez_default": "Juez Administrativo / Tribunal Administrativo",
    "cuerpos_normativos_minimos": ["CPACA", "CN"],
    "pretensiones_verbos_validos": ["DECLARAR", "RESTABLECER", "CONDENAR"],
    "requires_liquidacion": False,
    "requires_juramento": True,
    "requires_firma": True,
    "notas_calidad": [
        "Acto administrativo demandado debe estar identificado por número y fecha.",
        "Caducidad (4 meses Art. 164 CPACA) debe estar verificada implícitamente.",
        "Causales de nulidad del Art. 137 CPACA deben estar invocadas.",
    ],
}


# ============================================================
# CONSTITUCIONAL — TUTELA
# ============================================================

TUTELA: TemplateContract = {
    "doc_type": "tutela",
    "area": "constitucional",
    "descripcion": "Acción de tutela (Art. 86 CN + Decreto 2591/1991)",
    "secciones_obligatorias": [
        "encabezado", "partes", "derechos_vulnerados", "hechos",
        "pretensiones", "fundamentos", "pruebas", "notificaciones", "firma",
    ],
    # IMPORTANTE: tutela NO tiene juramento separado ni cuantía
    "secciones_no_aplican": ["juramento", "competencia_cuantia", "liquidacion"],
    "min_bloques_por_seccion": {
        "hechos": {"min": 3, "type": "hecho"},
        "pretensiones": {"min": 1, "type": "pretension"},
        "fundamentos": {"min": 2, "blocks_required": ["norma_citada", "jurisprudencia"]},
    },
    "juramento_norma_ref": "",  # no aplica
    "competencia_juez_default": "Juez de Tutela de Reparto",
    "cuerpos_normativos_minimos": ["CN", "Decreto 2591/1991"],
    "pretensiones_verbos_validos": ["TUTELAR", "ORDENAR", "AMPARAR"],
    "requires_liquidacion": False,
    "requires_juramento": False,
    "requires_firma": True,
    "notas_calidad": [
        "Derechos fundamentales vulnerados deben estar EXPLÍCITAMENTE listados (Art. 11, 13, 49 CN, etc.).",
        "Hechos deben demostrar acción/omisión de autoridad o particular.",
        "Subsidiariedad: debe argumentar por qué tutela y no otro mecanismo.",
        "Perjuicio irremediable si es tutela transitoria.",
        "Jurisprudencia hito de Corte Constitucional debe estar citada (T-/C-/SU-).",
    ],
}


# ============================================================
# REGISTRO Y LOOKUP
# ============================================================

TEMPLATE_CONTRACTS: dict[str, TemplateContract] = {
    # Civil
    "demanda_civil_ordinaria": CIVIL_ORDINARIA,
    "demanda_pertenencia": PERTENENCIA,
    "pertenencia": PERTENENCIA,
    "demanda_responsabilidad_civil": RESPONSABILIDAD_CIVIL,
    "demanda_civil_responsabilidad_extracontractual": RESPONSABILIDAD_CIVIL,
    # Familia
    "demanda_divorcio": DIVORCIO,
    "demanda_alimentos": ALIMENTOS,
    # Laboral
    "demanda_laboral_ordinaria": LABORAL_ORDINARIA,
    "demanda_laboral": LABORAL_ORDINARIA,
    # Administrativo
    "demanda_nulidad_restablecimiento": NULIDAD_RESTABLECIMIENTO,
    # Constitucional
    "tutela": TUTELA,
    "accion_tutela": TUTELA,
}


def get_contract(doc_type: str | None) -> TemplateContract:
    """Devuelve el contrato del doc_type. Si no está mapeado, retorna DEFAULT."""
    if not doc_type:
        return DEFAULT_CONTRACT
    return TEMPLATE_CONTRACTS.get(doc_type.lower().strip(), DEFAULT_CONTRACT)


def is_known_doc_type(doc_type: str | None) -> bool:
    """True si el doc_type tiene contrato específico (no es DEFAULT)."""
    if not doc_type:
        return False
    return doc_type.lower().strip() in TEMPLATE_CONTRACTS
