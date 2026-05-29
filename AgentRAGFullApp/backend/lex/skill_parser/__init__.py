"""Parser tolerante de SKILL.md.

Convierte el texto del SKILL.md (frontmatter YAML + secciones markdown)
en dataclasses tipados que el orchestrator y los builders consumen.

Sprint M19.31 (refactor arquitectónico):
  - skill_md_text → SkillFrontmatter + SkillBody
  - SkillBody.document_structure → list[SkillSection] con NOMBRES REALES
    de cláusulas ("PRIMERA. OBJETO DEL PODER") en lugar de keys genéricos
  - SkillBody.style_conventions → texto + reglas extraíbles
  - SkillBody.placeholders → list[Placeholder] del MD
  - SkillBody.style_hints → opcional, de "## docx-js Style Hints"

NO depende de pyyaml (parser minimalista inline) ni markdown (regex).
NO falla por SKILL.md mal formado: produce SkillBody parcial y log warning.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ============================================================================
# Dataclasses
# ============================================================================


@dataclass
class SkillFrontmatter:
    """YAML frontmatter parseado."""
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    doc_family: Optional[str] = None
    doc_type: Optional[str] = None
    default_scope: Optional[str] = None
    language: str = "es-CO"
    jurisdiction: str = "CO"
    version: Optional[str] = None
    tier: str = "public"
    sources: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillSection:
    """Una sección del Document Structure parseada del MD.

    Ejemplos:
      - title="PRIMERA. OBJETO DEL PODER", key="primera_objeto_del_poder", order=1
      - title="HECHOS", key="hechos", order=4
      - title="Encabezado", key="encabezado", order=0, sacramental=False
    """
    title: str             # Como aparece en el MD: "PRIMERA. OBJETO DEL PODER"
    key: str               # Slug derivado: "primera_objeto_del_poder"
    order: int             # 1-based de aparición en el MD
    sacramental: bool = False   # True si el MD lo marca como obligatorio
    instruction: Optional[str] = None  # Texto descriptivo si lo hay
    roman: Optional[str] = None        # "PRIMERA", "I", etc. extraído si lo hay


@dataclass
class SkillPlaceholder:
    """Un placeholder del MD ("Common Placeholders" o equivalente)."""
    key: str                # "[NOMBRE_PODERDANTE]" o "NOMBRE_PODERDANTE"
    description: Optional[str] = None
    example: Optional[str] = None
    type_hint: Optional[str] = None  # "string", "número", "fecha", etc.


@dataclass
class SkillStyleHints:
    """Hints específicos para el builder docx (opcional)."""
    font_family: str = "Arial"
    font_size_pt: int = 11
    heading1_size_pt: int = 14
    heading1_bold: bool = True
    heading1_color: str = "000000"
    heading1_alignment: str = "left"  # "left" | "center"
    page_margin_inches: float = 1.0
    page_size: str = "Letter"
    line_spacing: float = 1.15
    first_line_indent_inches: float = 0.5
    paragraph_after_pt: int = 6
    justify_body: bool = True


@dataclass
class SkillBody:
    """Cuerpo parseado del SKILL.md."""
    when_to_use: Optional[str] = None
    document_structure: list[SkillSection] = field(default_factory=list)
    style_conventions_raw: Optional[str] = None
    placeholders: list[SkillPlaceholder] = field(default_factory=list)
    style_hints: SkillStyleHints = field(default_factory=SkillStyleHints)
    risk_warnings_raw: Optional[str] = None
    bibliography_raw: Optional[str] = None
    other_sections: dict[str, str] = field(default_factory=dict)


@dataclass
class ParsedSkill:
    frontmatter: SkillFrontmatter
    body: SkillBody
    raw_md: str
    parse_warnings: list[str] = field(default_factory=list)


# ============================================================================
# YAML frontmatter parser (minimalista, sin dependencias)
# ============================================================================


_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _parse_yaml_minimal(yaml_text: str) -> dict[str, Any]:
    """Parser YAML mínimo para frontmatter de SKILL.md.

    Soporta:
      - key: value
      - key: "value with spaces"
      - key: |  (bloque multilinea indentado 2 espacios)
      - key:    (lista, items con "- ")
    NO soporta nesting profundo, mapping anidados, ni tipos complejos.
    """
    out: dict[str, Any] = {}
    if not yaml_text:
        return out
    lines = yaml_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        val = rest.strip()
        if val in ("|", ">"):
            # Bloque multilínea
            i += 1
            parts: list[str] = []
            while i < len(lines) and (lines[i].startswith("  ") or not lines[i].strip()):
                if lines[i].startswith("  "):
                    parts.append(lines[i][2:])
                else:
                    parts.append("")
                i += 1
            out[key] = "\n".join(parts).rstrip()
            continue
        if val == "":
            # Posible lista
            i += 1
            items: list[str] = []
            while i < len(lines) and lines[i].lstrip().startswith("- "):
                items.append(lines[i].lstrip()[2:].strip())
                i += 1
            out[key] = items
            continue
        # Valor simple
        out[key] = val.strip().strip('"').strip("'")
        i += 1
    return out


def _frontmatter_from_dict(d: dict[str, Any]) -> SkillFrontmatter:
    """Convierte el dict del YAML al dataclass tipado."""
    known = {"name","description","category","doc_family","doc_type","default_scope",
             "language","jurisdiction","version","tier","sources"}
    extra = {k: v for k, v in d.items() if k not in known}
    sources = d.get("sources") or []
    if isinstance(sources, str):
        sources = [sources]
    return SkillFrontmatter(
        name=d.get("name"),
        description=d.get("description"),
        category=d.get("category"),
        doc_family=d.get("doc_family"),
        doc_type=d.get("doc_type"),
        default_scope=d.get("default_scope"),
        language=d.get("language") or "es-CO",
        jurisdiction=(d.get("jurisdiction") or "CO").upper(),
        version=d.get("version"),
        tier=(d.get("tier") or "public").lower(),
        sources=list(sources) if isinstance(sources, list) else [],
        extra=extra,
    )


# ============================================================================
# Body parser (secciones markdown)
# ============================================================================


# Heading principal (## X) divide secciones del SKILL.md
_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _split_md_sections(md: str) -> dict[str, str]:
    """Divide el MD por headings ## y retorna {heading_lower: body}."""
    sections: dict[str, str] = {}
    matches = list(_H2_RE.finditer(md))
    for i, m in enumerate(matches):
        heading = m.group(1).strip().lower()
        # Normalizar variantes ("Document Structure (orden estricto)" → "document structure")
        heading_norm = re.sub(r"\(.*?\)", "", heading).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        body = md[start:end].strip()
        sections[heading_norm] = body
    return sections


def _slugify(s: str) -> str:
    """Convierte "PRIMERA. OBJETO DEL PODER" → "primera_objeto_del_poder"."""
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9áéíóúñ]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "section"


# Patrones de items numerados en "Document Structure"
# 1. **Título**: descripción
# 1. **PRIMERA. OBJETO** descripción
# 6. **PRIMERA. OBJETO DEL PODER** (texto descriptivo)
# - **ENCABEZADO**: ...
_ITEM_RE = re.compile(
    r"^\s*(?:\d+\.|\-|\*)\s+(?:\*\*([^*]+?)\*\*|([^:\n]+?))(?::\s*(.*))?\s*$",
    re.MULTILINE,
)


# Patrón para reconocer ordinales: PRIMERA, SEGUNDA, ..., DÉCIMA, UNDÉCIMA, etc.
# o numerales romanos I, II, III, IV, V (legacy)
_ORDINAL_RE = re.compile(
    r"^\s*(PRIMERA|SEGUNDA|TERCERA|CUARTA|QUINTA|SEXTA|SÉPTIMA|SEPTIMA|"
    r"OCTAVA|NOVENA|DÉCIMA|DECIMA|UNDÉCIMA|UNDECIMA|DUODÉCIMA|DUODECIMA|"
    r"DÉCIMA\s+TERCERA|PENÚLTIMA|PENULTIMA|ÚLTIMA|ULTIMA|"
    r"I|II|III|IV|V|VI|VII|VIII|IX|X|XI|XII|XIII)\b",
    re.IGNORECASE,
)


def _parse_document_structure(body: str) -> list[SkillSection]:
    """Parsea la sección "## Document Structure" en SkillSection[].

    Espera lista numerada o bullets con títulos en bold:
      1. **PRIMERA. OBJETO DEL PODER**
      2. **SEGUNDA. FACULTAD DE REPRESENTACIÓN**

    O variantes:
      - **ENCABEZADO**: ...
      1. Encabezado (notarial)

    Devuelve sections con title=texto literal, key=slug, order=1-based.
    """
    sections: list[SkillSection] = []
    if not body:
        return sections

    order = 0
    for m in _ITEM_RE.finditer(body):
        order += 1
        # group(1) = título en **bold**, group(2) = título sin bold,
        # group(3) = descripción después de ":"
        title_bold = m.group(1)
        title_plain = m.group(2)
        instruction = (m.group(3) or "").strip() or None
        raw_title = (title_bold or title_plain or "").strip()
        if not raw_title:
            continue
        # Limpiar artefactos: "PRIMERA. OBJETO DEL PODER (CRÍTICA)" → mantener "PRIMERA. OBJETO DEL PODER (CRÍTICA)"
        # No quitar paréntesis aquí, son parte del título
        # Detectar ordinal
        ord_m = _ORDINAL_RE.match(raw_title)
        roman = ord_m.group(1).upper() if ord_m else None
        # sacramental si tiene marcadores conocidos
        sacramental = bool(re.search(r"\b(CRÍTICA|CRITICA|OBLIGATORI|SACRAMENTAL)\b",
                                      raw_title, re.IGNORECASE))
        sections.append(SkillSection(
            title=raw_title,
            key=_slugify(raw_title)[:60],
            order=order,
            sacramental=sacramental,
            instruction=instruction,
            roman=roman,
        ))
    return sections


# Placeholders típicos:
# - `[NOMBRE_PODERDANTE]`: string, MAYÚSCULAS
# | `[NOMBRE_PODERDANTE]` | string MAYÚSCULAS | LAURA ALEJANDRA ALZATE ... |
# `[CC_PODERDANTE]` (número)
_PLACEHOLDER_RE = re.compile(r"`\[([A-Z0-9_]+)\]`")
_PLACEHOLDER_ROW_RE = re.compile(
    r"\|\s*`\[([A-Z0-9_]+)\]`\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|"
)


def _parse_placeholders(body: str) -> list[SkillPlaceholder]:
    """Extrae placeholders del cuerpo del MD.

    Prioriza filas de tabla "| placeholder | type | example |".
    Si no hay tabla, busca placeholders sueltos como `[XXX]`.
    """
    placeholders: dict[str, SkillPlaceholder] = {}
    if not body:
        return []
    # Tabla
    for m in _PLACEHOLDER_ROW_RE.finditer(body):
        key, type_h, example = m.group(1), m.group(2).strip(), m.group(3).strip()
        placeholders[key] = SkillPlaceholder(
            key=key, type_hint=type_h, example=example,
        )
    # Backticks sueltos (si no fueron capturados por tabla)
    for m in _PLACEHOLDER_RE.finditer(body):
        key = m.group(1)
        if key not in placeholders:
            placeholders[key] = SkillPlaceholder(key=key)
    return list(placeholders.values())


# Style hints en "## docx-js Style Hints":
# - Tamaño hoja: Letter
# - Fuente: Arial 11pt
# - **Heading1** (cláusulas): `size: 26, bold, spacing: { before: 240, after: 200 }`
_STYLE_FONT_RE = re.compile(r"fuente?\s*[:=]\s*([\w\s]+?)(?:\s*(\d+)\s*pt)?", re.IGNORECASE)
_STYLE_PAGESIZE_RE = re.compile(r"(?:tama[nñ]o\s+(?:de\s+)?hoja|page\s*size)\s*[:=]\s*([\w\s]+)", re.IGNORECASE)
_STYLE_MARGIN_RE = re.compile(r"m[aá]rgen[es]*\s*[:=]\s*([\d.]+)\s*(?:in|inch|pulgada)", re.IGNORECASE)
_STYLE_H1_ALIGN_RE = re.compile(r"heading1?[^\n]*?alignment\s*[:=]\s*['\"]?(left|center|centro|izquierda)", re.IGNORECASE)


def _parse_style_hints(body: str) -> SkillStyleHints:
    """Parsea hints del bloque "## docx-js Style Hints".
    Si no encuentra info, devuelve defaults conservadores.
    """
    hints = SkillStyleHints()
    if not body:
        return hints
    # Fuente
    m = _STYLE_FONT_RE.search(body)
    if m:
        font_text = (m.group(1) or "").strip().rstrip(",").strip()
        if font_text and len(font_text) <= 40:
            # Solo primera palabra significativa
            hints.font_family = font_text.split()[0].strip(",")
        if m.group(2):
            try:
                hints.font_size_pt = int(m.group(2))
            except ValueError:
                pass
    # Tamaño hoja
    m = _STYLE_PAGESIZE_RE.search(body)
    if m:
        ps = m.group(1).strip().lower()
        if "letter" in ps:
            hints.page_size = "Letter"
        elif "a4" in ps:
            hints.page_size = "A4"
        elif "legal" in ps:
            hints.page_size = "Legal"
    # Márgenes
    m = _STYLE_MARGIN_RE.search(body)
    if m:
        try:
            v = float(m.group(1))
            if 0.3 <= v <= 3.0:
                hints.page_margin_inches = v
        except ValueError:
            pass
    # Alignment heading1
    m = _STYLE_H1_ALIGN_RE.search(body)
    if m:
        al = m.group(1).lower()
        hints.heading1_alignment = "center" if al in ("center", "centro") else "left"
    return hints


# ============================================================================
# Top-level parse function
# ============================================================================


def parse_skill_md(md_text: str) -> ParsedSkill:
    """Parser principal. Nunca tira excepción: en caso de error, devuelve
    ParsedSkill con parse_warnings explicando qué falló."""
    warnings: list[str] = []
    md_text = md_text or ""
    if not md_text.strip():
        return ParsedSkill(
            frontmatter=SkillFrontmatter(),
            body=SkillBody(),
            raw_md="",
            parse_warnings=["empty_md"],
        )

    # 1. Frontmatter
    m = _FM_RE.match(md_text.strip())
    fm_dict: dict[str, Any] = {}
    body_md = md_text
    if m:
        fm_text = m.group(1)
        body_md = m.group(2)
        try:
            fm_dict = _parse_yaml_minimal(fm_text)
        except Exception as e:
            warnings.append(f"frontmatter_parse_failed:{e}")
    else:
        warnings.append("no_frontmatter")

    frontmatter = _frontmatter_from_dict(fm_dict)

    # 2. Secciones
    sections_md = _split_md_sections(body_md)
    # Buscar "document structure" tolerante
    doc_struct_body = ""
    for k, v in sections_md.items():
        kn = k.lower()
        if "document structure" in kn or "estructura del documento" in kn:
            doc_struct_body = v
            break
    style_conv_body = ""
    for k, v in sections_md.items():
        if "style conventions" in k or "convenciones de estilo" in k:
            style_conv_body = v
            break
    placeholders_body = ""
    for k, v in sections_md.items():
        if "placeholders" in k or "common placeholders" in k or "marcadores" in k:
            placeholders_body = v
            break
    style_hints_body = ""
    for k, v in sections_md.items():
        if "style hints" in k or "docx-js style" in k:
            style_hints_body = v
            break
    risk_body = ""
    for k, v in sections_md.items():
        if "risk warnings" in k or "advertencias" in k:
            risk_body = v
            break
    biblio_body = ""
    for k, v in sections_md.items():
        if "bibliography" in k or "bibliografía" in k or "bibliografia" in k:
            biblio_body = v
            break
    when_to_use = ""
    for k, v in sections_md.items():
        if "when to use" in k or "cuándo usar" in k or "cuando usar" in k:
            when_to_use = v
            break

    document_structure = _parse_document_structure(doc_struct_body)
    if not document_structure:
        warnings.append("empty_document_structure")
    placeholders = _parse_placeholders(placeholders_body or style_conv_body)
    style_hints = _parse_style_hints(style_hints_body or style_conv_body)

    other = {
        k: v for k, v in sections_md.items()
        if not any(s in k for s in (
            "document structure", "style conventions", "placeholders",
            "style hints", "risk warnings", "advertencias",
            "bibliography", "when to use",
            "cuándo usar", "cuando usar",
        ))
    }

    body = SkillBody(
        when_to_use=when_to_use or None,
        document_structure=document_structure,
        style_conventions_raw=style_conv_body or None,
        placeholders=placeholders,
        style_hints=style_hints,
        risk_warnings_raw=risk_body or None,
        bibliography_raw=biblio_body or None,
        other_sections=other,
    )

    return ParsedSkill(
        frontmatter=frontmatter,
        body=body,
        raw_md=md_text,
        parse_warnings=warnings,
    )


__all__ = [
    "SkillFrontmatter",
    "SkillSection",
    "SkillPlaceholder",
    "SkillStyleHints",
    "SkillBody",
    "ParsedSkill",
    "parse_skill_md",
]
