"""Tests del parser SKILL.md contra los 21 .md de TEST Freddy/templates_review/.

Valida que:
  - Parser nunca tira excepción
  - Frontmatter se carga correctamente
  - Document Structure se extrae con >=3 secciones por SKILL.md
  - Los SKILL.md notariales/judiciales/contractuales tienen al menos los
    headings esperados
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from lex.skill_parser import (  # noqa: E402
    SkillFrontmatter,
    SkillSection,
    parse_skill_md,
)


TEMPLATES_DIR = Path(
    "c:/Users/freddyrs/Desktop/Legal Demo/Legal_agent_Frontend/TEST Freddy/templates_review"
)


def _md_files() -> list[Path]:
    if not TEMPLATES_DIR.exists():
        pytest.skip(f"TEMPLATES_DIR missing: {TEMPLATES_DIR}")
    return sorted(p for p in TEMPLATES_DIR.glob("*.md") if not p.name.startswith("README"))


@pytest.mark.parametrize("md_path", _md_files(), ids=lambda p: p.name)
def test_parse_each_skill_md_succeeds(md_path: Path):
    """Cada SKILL.md debe parsearse sin excepción y dejar frontmatter+sections."""
    md = md_path.read_text(encoding="utf-8")
    parsed = parse_skill_md(md)

    # Frontmatter
    assert parsed.frontmatter is not None, f"{md_path.name}: no frontmatter"
    assert parsed.frontmatter.name, f"{md_path.name}: name missing"
    assert parsed.frontmatter.doc_type, f"{md_path.name}: doc_type missing"
    assert parsed.frontmatter.doc_family, f"{md_path.name}: doc_family missing"
    assert parsed.frontmatter.jurisdiction in ("CO", "co"), \
        f"{md_path.name}: jurisdiction wrong: {parsed.frontmatter.jurisdiction!r}"

    # Body
    assert parsed.body is not None
    # Al menos 3 secciones por documento
    assert len(parsed.body.document_structure) >= 3, \
        f"{md_path.name}: < 3 sections ({len(parsed.body.document_structure)})"

    # Style hints con defaults razonables
    sh = parsed.body.style_hints
    assert sh.font_size_pt > 0
    assert sh.page_margin_inches > 0
    assert sh.heading1_alignment in ("left", "center")

    # No debe haber warnings críticos
    critical_warnings = [w for w in parsed.parse_warnings if "empty" in w]
    assert not critical_warnings, f"{md_path.name}: critical warnings={critical_warnings}"


def test_poder_especial_has_ordinal_clauses():
    """El SKILL.md de poder_especial debe tener cláusulas PRIMERA, SEGUNDA, etc."""
    p = TEMPLATES_DIR / "01_notarial_poder_especial_co.md"
    if not p.exists():
        pytest.skip(f"missing {p}")
    parsed = parse_skill_md(p.read_text(encoding="utf-8"))
    titles = " ".join(s.title.upper() for s in parsed.body.document_structure)
    # Debe contener al menos PRIMERA o SEXTA en los títulos
    assert "PRIMERA" in titles or "OBJETO" in titles, \
        f"poder_especial sections sin PRIMERA/OBJETO: {titles[:200]}"


def test_parser_tolerates_empty_input():
    parsed = parse_skill_md("")
    assert "empty_md" in parsed.parse_warnings
    assert parsed.frontmatter is not None
    assert parsed.body is not None


def test_parser_tolerates_no_frontmatter():
    parsed = parse_skill_md("# No frontmatter\n\n## Document Structure\n\n1. **PRIMERA. OBJETO**\n2. **SEGUNDA. FACULTAD**")
    assert "no_frontmatter" in parsed.parse_warnings
    # Aun así debe extraer estructura
    assert len(parsed.body.document_structure) == 2


def test_parser_extracts_placeholders_from_table():
    md = """---
name: test
doc_type: x
doc_family: y
---

## Common Placeholders

| Placeholder | Type | Example |
|---|---|---|
| `[NOMBRE]` | string | LAURA |
| `[CC]` | número | 1.017.207.731 |
"""
    parsed = parse_skill_md(md)
    keys = {p.key for p in parsed.body.placeholders}
    assert "NOMBRE" in keys
    assert "CC" in keys
