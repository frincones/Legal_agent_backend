"""Block schema Pydantic — Document Generation v3.1.

Vocabulario universal de bloques tipados que reemplaza el markdown streaming.
Mismo árbol de bloques se renderiza en el canvas (frontend) y se serializa a
.docx forense (backend), garantizando WYSIWYG.

Diseño:
- Cada Block hereda de BaseBlock con campos comunes (block_id, type)
- Discriminated union por campo `type` (Pydantic v2 style)
- Runs = fragmentos de texto con formato (bold/italic/underline) para inline mix
"""
from __future__ import annotations

import uuid
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


# ============================================================
# Helpers
# ============================================================

def new_block_id() -> str:
    """Genera un block_id único (referencia externa para SSE + persistencia)."""
    return f"blk_{uuid.uuid4().hex[:16]}"


# ============================================================
# Run — fragmento de texto con formato inline
# ============================================================

class Run(BaseModel):
    """Fragmento de texto con formato. Múltiples runs componen un párrafo."""
    model_config = ConfigDict(extra="forbid")

    text: str = Field(default="", description="Texto literal del fragmento")
    bold: bool = Field(default=False)
    italic: bool = Field(default=False)
    underline: bool = Field(default=False)


# ============================================================
# BaseBlock + concrete blocks (discriminated union)
# ============================================================

Alignment = Literal["justify", "left", "right", "center"]


class _BaseBlock(BaseModel):
    """Base abstracta para todos los Block. NO usar directamente."""
    model_config = ConfigDict(extra="forbid")

    block_id: str = Field(default_factory=new_block_id)


class TitleBlock(_BaseBlock):
    """Título principal centrado (ej. 'DEMANDA ORDINARIA LABORAL')."""
    type: Literal["title"] = "title"
    text: str
    level: Literal[0, 1, 2] = 0  # 0 = title doc, 1 = title sección, 2 = subtítulo


class SectionHeadingBlock(_BaseBlock):
    """Heading de sección con numeración romana (ej. 'I. PARTES DEL PROCESO')."""
    type: Literal["section_heading"] = "section_heading"
    # M20.14 fix: el Brain a veces emite section_heading sin roman/section_key
    # (e.g. para "ANEXOS" o headings sin numeración). Permitir defaults vacíos
    # para no perder el block por pydantic validation.
    roman: str = Field(default="")  # 'I', 'II', 'III', ... o '' si sin numeración
    text: str
    section_key: str = Field(default="")  # 'partes', 'hechos', etc


class SubsectionBlock(_BaseBlock):
    """Subsección numerada (ej. '1.1. PARTE DEMANDANTE')."""
    type: Literal["subsection"] = "subsection"
    number: str  # '1.1', '4.2', etc
    text: str


class ParagraphBlock(_BaseBlock):
    """Párrafo con uno o más runs."""
    type: Literal["paragraph"] = "paragraph"
    runs: list[Run]
    align: Alignment = "justify"
    indent_left_cm: float | None = None


class HechoBlock(_BaseBlock):
    """Hecho numerado con hanging indent forense."""
    type: Literal["hecho"] = "hecho"
    num: int
    runs: list[Run]


class PretensionBlock(_BaseBlock):
    """Pretensión con ordinal romano (PRIMERA, SEGUNDA, DÉCIMA TERCERA...)."""
    type: Literal["pretension"] = "pretension"
    ord: str
    runs: list[Run]
    kind: Literal["declarativa", "condena", "general"] = "general"


class NormaCitadaBlock(_BaseBlock):
    """Cita de norma con badge de vigencia + texto literal opcional."""
    type: Literal["norma_citada"] = "norma_citada"
    norma: str  # 'Art. 64 CST' o 'Ley 50/90 Art. 99'
    contenido: list[Run] = Field(default_factory=list)
    verified: bool = False
    derogada: bool = False
    fuente_ref: str | None = None  # 'cst:64' o url (legacy)
    # M19.10.A7: URLs canónicas verificadas (propagadas desde VerificationVerdict)
    fuente_url: str | None = None  # URL principal validada
    fuente_url_vigente: str | None = None  # si derogada, URL de norma vigente
    discovered_by: str | None = None  # 'brave_search'|'internal_db'|'pattern'|...


class JurisprudenciaBlock(_BaseBlock):
    """Cita de jurisprudencia con M.P. + ratio decidendi literal."""
    type: Literal["jurisprudencia"] = "jurisprudencia"
    id: str  # 'SL1430-2022', 'C-1507/2000', 'T-760/2008'
    mp: str  # 'Iván Mauricio Lenis Gómez'
    corte: str  # 'CSJ Sala Laboral' | 'Corte Constitucional' | ...
    fecha: str | None = None  # ISO date
    ratio: list[Run] = Field(default_factory=list)
    chunk_id: str | None = None
    verified: bool = False
    sim_score: float | None = None
    # M19.10.A3: URL canónica para hyperlink en DOCX (propagada de VerificationVerdict)
    fuente_url: str | None = None
    discovered_by: str | None = None


class SilogismoBlock(_BaseBlock):
    """Razonamiento silogístico explícito: Premisa Mayor → Menor → Conclusión."""
    type: Literal["silogismo"] = "silogismo"
    premisa_mayor: list[Run]
    premisa_menor: list[Run]
    conclusion: list[Run]


class TableBlock(_BaseBlock):
    """Tabla con header + rows. Soporta shading custom para celdas críticas."""
    type: Literal["table"] = "table"
    header: list[str]
    rows: list[list[str]]
    header_shading: str | None = "D9E1F2"  # azul claro
    total_row_shading: str | None = "F2F2F2"  # gris para fila TOTAL
    has_total_row: bool = False


class CalcStepBlock(_BaseBlock):
    """Paso de cálculo: fórmula + aplicación + total."""
    type: Literal["calc_step"] = "calc_step"
    label: str  # '7.1. Indemnización por despido sin justa causa (Art. 64 CST)'
    formula: str  # 'Indemnización = (Salario/30) × [30 + 20×(años-1)]'
    aplicacion: str  # 'Indemnización = ($2.500.000/30) × [30 + 20×(7-1)]'
    total: str  # 'TOTAL INDEMNIZACIÓN = $12.708.333'


class ListItemBlock(_BaseBlock):
    """Item de lista numerada/alfanumérica (anexos, pruebas documentales, etc)."""
    type: Literal["list_item"] = "list_item"
    kind: Literal["anexo", "documental", "testimonial", "pericial", "generic"] = "generic"
    num: str  # '1', 'a', 'i'
    runs: list[Run]


class JuramentoBlock(_BaseBlock):
    """Sección de juramento (Art. 28 CPTSS, Art. 37 D. 2591/91, etc)."""
    type: Literal["juramento"] = "juramento"
    text: str
    norma_ref: str | None = None  # 'Art. 28 num. 7 CPTSS'


class FirmaBlock(_BaseBlock):
    """Bloque final de firma. M19.24.D.6: extendido para soportar 5 variantes
    de cierre además del clásico apoderado judicial.

    Variantes (cierre_tipo):
      - firma_apoderado_judicial (default): Atentamente + T.P. C.S.J.
      - firma_partes_notarial: EL PODERDANTE / EL APODERADO (ACEPTO) con
        dos firmas separadas. Puede combinarse con diligencia_notarial.
      - firma_natural: solo nombre + CC sin T.P.
      - diligencia_notarial: bloque adicional con espacio reservado notaría
      - firma_consultor: Cordialmente + cargo + empresa
      - firma_representante_legal: Rep Legal + NIT + sociedad
      - firma_partes_contractuales: LAS PARTES con N firmas
      - firma_corporativa_organos: Presidente + Secretario de asamblea
    """
    type: Literal["firma"] = "firma"
    ciudad_fecha: str
    nombre: str
    tp: str = ""
    cc: str | None = None
    email: str | None = None
    telefono: str | None = None
    # M19.24.D.6 — variante de cierre. Default mantiene compat con apoderado judicial
    cierre_tipo: str | None = None
    # Para firmas con múltiples partes (notarial, contractual, corporativa)
    parties: list[dict] | None = None  # [{rol, nombre, cc, cargo, ...}]
    # Para firma representante legal: identifica la sociedad
    razon_social: str | None = None
    nit_sociedad: str | None = None
    cargo: str | None = None
    # Para diligencia notarial / consultor
    detalle_adicional: str | None = None


class BlankBlock(_BaseBlock):
    """Párrafo en blanco (espaciado forense)."""
    type: Literal["blank"] = "blank"


# ============================================================
# Discriminated union
# ============================================================

Block = Annotated[
    Union[
        TitleBlock,
        SectionHeadingBlock,
        SubsectionBlock,
        ParagraphBlock,
        HechoBlock,
        PretensionBlock,
        NormaCitadaBlock,
        JurisprudenciaBlock,
        SilogismoBlock,
        TableBlock,
        CalcStepBlock,
        ListItemBlock,
        JuramentoBlock,
        FirmaBlock,
        BlankBlock,
    ],
    Field(discriminator="type"),
]


BlockType = Literal[
    "title",
    "section_heading",
    "subsection",
    "paragraph",
    "hecho",
    "pretension",
    "norma_citada",
    "jurisprudencia",
    "silogismo",
    "table",
    "calc_step",
    "list_item",
    "juramento",
    "firma",
    "blank",
]


# ============================================================
# Helpers para construir bloques desde código (orquestador)
# ============================================================

def run(text: str, bold: bool = False, italic: bool = False, underline: bool = False) -> Run:
    """Atajo para construir un Run."""
    return Run(text=text, bold=bold, italic=italic, underline=underline)


def runs_from_text(text: str) -> list[Run]:
    """Convierte un string plano en lista de runs (un solo run sin formato)."""
    return [Run(text=text)]
