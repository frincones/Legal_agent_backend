"""Base de TemplateDef — define la estructura declarativa de cada doc_type.

Cada doc_type (12 inicial) tiene su propio TemplateDef que parametriza el motor
orchestrator. El motor es uno solo, los templates son data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class SectionDef:
    """Definición de una sección del documento."""
    key: str  # 'partes', 'hechos', etc — estable, usado en code
    title: str  # 'PARTES DEL PROCESO'
    order: int  # orden visual (1, 2, 3...)
    roman: str | None = None  # 'I', 'II', None si no es numerada
    required: bool = True
    # Pista al LLM sobre qué tipos de bloques generar prioritariamente
    expected_blocks: list[str] = field(default_factory=list)
    # Instrucción específica para esta sección (override del system prompt)
    section_instruction: str | None = None


@dataclass
class HunterQuery:
    """Query que el JurisprudenceHunter ejecuta para esta plantilla."""
    query: str
    hunter: str = "general"  # 'sala_laboral_csj' | 'corte_constitucional' | 'normas_codigos' | ...
    top_k: int = 3
    min_similarity: float = 0.6


@dataclass
class Rule:
    """Regla de validación post-generación."""
    name: str
    kind: Literal["has_section", "min_hechos", "min_pretensiones", "cuantia_min_smlmv",
                  "cita_norma_minimo", "cita_jurisprudencia_minimo", "has_juramento",
                  "has_firma", "custom"] = "custom"
    value: Any = None
    severity: Literal["error", "warning"] = "warning"

    @staticmethod
    def has_section(key: str) -> "Rule":
        return Rule(name=f"has_section:{key}", kind="has_section", value=key)

    @staticmethod
    def min_hechos(n: int) -> "Rule":
        return Rule(name=f"min_hechos:{n}", kind="min_hechos", value=n)

    @staticmethod
    def min_pretensiones(n: int) -> "Rule":
        return Rule(name=f"min_pretensiones:{n}", kind="min_pretensiones", value=n)

    @staticmethod
    def cita_minimo(refs: list[str]) -> "Rule":
        return Rule(name=f"cita_minimo:{refs}", kind="cita_norma_minimo", value=refs)


@dataclass
class DetailProfile:
    """Perfil de detalle por doc_type — garantiza calidad uniforme con estructura distinta."""
    min_paragraphs_per_section: int = 3
    min_hechos: int = 0  # 0 = no aplica
    min_pretensiones: int = 0
    require_silogismo: bool = False
    require_calculos: bool = False
    require_juramento: bool = False
    require_pretensiones_romanas: bool = False
    require_table_resumen: bool = False
    jurisprudencia_min: int = 0
    normas_min: int = 0
    tone: Literal["solemne_forense", "solemne_constitucional", "comercial_formal",
                  "doctrinal", "petitorio_administrativo"] = "solemne_forense"


@dataclass
class ForensicStructure:
    """Configuración de formato visual (afecta docx export y canvas)."""
    style: Literal["demanda", "tutela", "contrato", "denuncia", "recurso",
                   "concepto", "poder", "petición"] = "demanda"
    page_margins_cm: tuple[float, float, float, float] = (3.0, 3.0, 3.0, 2.5)  # top, bot, left, right
    font_family: str = "Times New Roman"
    font_size_pt: int = 12
    line_spacing: float = 1.5
    pretensiones_label: str = "PRIMERA"  # 'PRIMERA' | 'PRIMERO' | 'PUNTO PRIMERO'
    show_juramento_section: bool = True
    show_firma_section: bool = True


@dataclass
class TemplateDef:
    """Definición completa de un template legal."""
    id: str  # 'demanda_laboral_ordinaria'
    nombre: str  # 'Demanda Ordinaria Laboral'
    jurisdiccion: str  # 'laboral' | 'civil' | 'penal' | ...
    materia: str = ""  # descripción libre
    version: int = 1

    sections_plan: list[SectionDef] = field(default_factory=list)
    hunters: list[HunterQuery] = field(default_factory=list)
    validation_rules: list[Rule] = field(default_factory=list)
    detail_profile: DetailProfile = field(default_factory=DetailProfile)
    forensic_structure: ForensicStructure = field(default_factory=ForensicStructure)

    # Schema de datos requeridos del brief (clave → tipo Python como string)
    required_data: dict[str, str] = field(default_factory=dict)

    # Función Python para invocar calculadora (None si no aplica)
    # Será una referencia a calc/laboral.py:full_liquidacion, etc.
    calculadora: str | None = None  # 'lex.calc.laboral:full_liquidacion'

    # Descripción libre del template
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serializa para persistir en template_catalog."""
        return {
            "id": self.id,
            "nombre": self.nombre,
            "jurisdiccion": self.jurisdiccion,
            "materia": self.materia,
            "version": self.version,
            "sections_plan": [
                {
                    "key": s.key, "title": s.title, "order": s.order, "roman": s.roman,
                    "required": s.required, "expected_blocks": s.expected_blocks,
                    "section_instruction": s.section_instruction,
                }
                for s in self.sections_plan
            ],
            "hunters": [
                {"query": h.query, "hunter": h.hunter, "top_k": h.top_k,
                 "min_similarity": h.min_similarity}
                for h in self.hunters
            ],
            "validation_rules": [
                {"name": r.name, "kind": r.kind, "value": r.value, "severity": r.severity}
                for r in self.validation_rules
            ],
            "detail_profile": {
                "min_paragraphs_per_section": self.detail_profile.min_paragraphs_per_section,
                "min_hechos": self.detail_profile.min_hechos,
                "min_pretensiones": self.detail_profile.min_pretensiones,
                "require_silogismo": self.detail_profile.require_silogismo,
                "require_calculos": self.detail_profile.require_calculos,
                "require_juramento": self.detail_profile.require_juramento,
                "require_pretensiones_romanas": self.detail_profile.require_pretensiones_romanas,
                "require_table_resumen": self.detail_profile.require_table_resumen,
                "jurisprudencia_min": self.detail_profile.jurisprudencia_min,
                "normas_min": self.detail_profile.normas_min,
                "tone": self.detail_profile.tone,
            },
            "forensic_structure": {
                "style": self.forensic_structure.style,
                "page_margins_cm": list(self.forensic_structure.page_margins_cm),
                "font_family": self.forensic_structure.font_family,
                "font_size_pt": self.forensic_structure.font_size_pt,
                "line_spacing": self.forensic_structure.line_spacing,
                "pretensiones_label": self.forensic_structure.pretensiones_label,
                "show_juramento_section": self.forensic_structure.show_juramento_section,
                "show_firma_section": self.forensic_structure.show_firma_section,
            },
            "required_data": self.required_data,
            "calculadora": self.calculadora,
            "description": self.description,
        }
