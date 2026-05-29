"""SkillContext — paquete de información del SKILL.md que viaja por el pipeline.

Producido por `skill_loader` (stage 1.4) y consumido por:
  - structure_discovery (skip LLM si tiene sections_plan)
  - block_generator (section_title real + style_conventions inyectado)
  - data_completeness_gate (placeholders del SKILL.md)
  - python_docx_builder (style_hints aplicados al .docx)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from lex.skill_parser import (
    SkillFrontmatter,
    SkillPlaceholder,
    SkillSection,
    SkillStyleHints,
)


@dataclass
class SkillContext:
    """Contexto del SKILL.md cargado para un doc_type específico."""

    # Origen
    doc_type: str
    skill_id: Optional[str] = None           # UUID de firm_skills.id si vino de BD
    skill_command: Optional[str] = None      # ej. "/redactar/poder-especial"
    firm_id: Optional[str] = None            # None = builtin
    source: str = "firm_skills"              # "firm_skills" | "inferred" | "fallback"

    # Datos parseados del SKILL.md
    frontmatter: Optional[SkillFrontmatter] = None
    sections: list[SkillSection] = field(default_factory=list)
    style_conventions_raw: Optional[str] = None
    placeholders: list[SkillPlaceholder] = field(default_factory=list)
    style_hints: Optional[SkillStyleHints] = None
    risk_warnings_raw: Optional[str] = None

    # Texto crudo del system_prompt / references_md (para inyectar al LLM si hace falta)
    raw_system_prompt: Optional[str] = None
    raw_references_md: Optional[str] = None

    parse_warnings: list[str] = field(default_factory=list)

    # ----- accesores -----

    @property
    def doc_family(self) -> Optional[str]:
        return self.frontmatter.doc_family if self.frontmatter else None

    @property
    def has_structure(self) -> bool:
        """True si tiene un Document Structure con >=3 secciones útiles."""
        return len([s for s in self.sections if s.title.strip()]) >= 3

    def to_sections_plan(self) -> list[dict[str, Any]]:
        """Convierte sections → formato esperado por orchestrator/structure_discovery.

        Formato compatible con el `plan` que consume el block_generator:
          [{key, title, order, roman, expected_blocks?, section_instruction?}, ...]
        """
        out: list[dict[str, Any]] = []
        for i, s in enumerate(self.sections, start=1):
            out.append({
                "key": s.key or f"section_{i}",
                "title": s.title,
                "order": s.order or i,
                "roman": s.roman,          # "PRIMERA" o "I" o None
                "section_instruction": s.instruction or "",
                "expected_blocks": [],
                "sacramental": s.sacramental,
            })
        return out

    def style_directive_text(self) -> str:
        """Texto resumido del estilo para inyectar en system_prompt del block_generator.

        Le dice al LLM EXPLÍCITAMENTE qué nombres de cláusulas usar y qué estilo.
        Evita que el modelo invente "I./II./III." cuando el template dice
        "PRIMERA, SEGUNDA, ...".
        """
        lines: list[str] = [
            "=== ESTILO OBLIGATORIO PARA ESTE DOCUMENTO ===",
        ]
        if self.frontmatter and self.frontmatter.name:
            lines.append(f"Tipo de documento: {self.frontmatter.name}")
        if self.doc_family:
            lines.append(f"Familia: {self.doc_family}")
        if self.sections:
            lines.append("")
            lines.append("Sigue EXACTAMENTE esta estructura, en orden, con los")
            lines.append("nombres LITERALES de cada sección como aparecen abajo:")
            for s in self.sections[:20]:
                lines.append(f"  {s.order}. {s.title}")
        if self.style_conventions_raw:
            # Compactar a un párrafo
            sc = self.style_conventions_raw.strip()
            if len(sc) > 2000:
                sc = sc[:2000] + "…"
            lines.append("")
            lines.append("Convenciones de estilo (resumen):")
            lines.append(sc)
        lines.append("")
        lines.append(
            "REGLAS:\n"
            "- NO uses numeración romana (I./II./III./IV.) salvo que el template\n"
            "  prescriba explícitamente esa numeración.\n"
            "- NO inventes encabezados como 'PARTES', 'PODERDANTE', 'APODERADO'\n"
            "  como secciones separadas si el template usa cláusulas ordinales\n"
            "  (PRIMERA. OBJETO, SEGUNDA. FACULTAD, …).\n"
            "- Respeta el orden y los nombres literales arriba.\n"
            "- Si data del usuario no incluye un campo, deja `[CAMPO]` literal.\n"
        )
        return "\n".join(lines)

    def to_audit_dict(self) -> dict[str, Any]:
        """Para logs/SSE/audit (sin contenidos PII gigantes)."""
        return {
            "doc_type": self.doc_type,
            "doc_family": self.doc_family,
            "skill_id": self.skill_id,
            "skill_command": self.skill_command,
            "firm_id": self.firm_id,
            "source": self.source,
            "sections_count": len(self.sections),
            "placeholders_count": len(self.placeholders),
            "has_structure": self.has_structure,
            "parse_warnings": self.parse_warnings,
        }


__all__ = ["SkillContext"]
