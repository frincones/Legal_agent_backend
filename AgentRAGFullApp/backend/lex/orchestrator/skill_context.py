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

        Filtra SOLO secciones clausulares (las que van a tener heading numerado
        en el documento). Items meta del MD ("Título", "Subtítulo italic",
        "Destinatario notarial", "Comparecencia del poderdante", "Designación
        del apoderado", "Lugar, fecha y firmas", "Diligencia notarial", etc.)
        quedan FUERA del plan: no son cláusulas, son lineamientos editoriales.

        Asigna ordinales secuenciales (PRIMERA, SEGUNDA, ..., DÉCIMA, ÚLTIMA)
        a las cláusulas que no tengan uno propio detectado por el parser.

        Formato compatible con el `plan` que consume el block_generator:
          [{key, title, order, roman, expected_blocks?, section_instruction?}, ...]
        """
        import re as _re

        # Palabras clave que indican que una sección del MD es clausular
        clausular_kw = _re.compile(
            r"^\s*(PRIMERA|SEGUNDA|TERCERA|CUARTA|QUINTA|SEXTA|S[EÉ]PTIMA|"
            r"OCTAVA|NOVENA|D[EÉ]CIMA|UND[EÉ]CIMA|DUOD[EÉ]CIMA|"
            r"PEN[UÚ]LTIMA|[UÚ]LTIMA|"
            r"HECHOS|PRETENSIONES|FUNDAMENTOS|OBJETO|"
            r"ANTECEDENTES|PARTES|PETICIONES|VIGENCIA|FACULTADES|"
            r"CL[AÁ]USULA|CLAUSE|ART[IÍ]CULO|"
            r"I|II|III|IV|V|VI|VII|VIII|IX|X|XI|XII|XIII)[\.\s]",
            _re.IGNORECASE,
        )
        # Patrones de items meta del MD que NO son cláusulas
        # NOTA: "N. Vigencia" y "N+1. Aceptación" no son meta — son cláusulas
        # con placeholder ordinal. Las dejamos pasar y luego les asignamos
        # el ordinal real (SEPTIMA, OCTAVA, ...).
        meta_kw = _re.compile(
            r"^\s*(T[ií]tulo|Subt[ií]tulo|Destinatario\s+notarial|"
            r"Comparecencia|Designaci[oó]n|Lugar[,\s]+fecha|"
            r"Lugar\s+y\s+fecha|^Firmas\b|Diligencia\s+notarial|"
            r"Encabezado(\s+\(notarial\))?)\b",
            _re.IGNORECASE,
        )
        # Patrón para limpiar placeholders "N." / "N+1." al inicio de un title
        # ("N. Vigencia y revocabilidad" → "Vigencia y revocabilidad")
        placeholder_n_re = _re.compile(
            r"^\s*N(?:\s*\+\s*\d+)?\.\s*",
            _re.IGNORECASE,
        )

        # 1. Filtrar candidatos: cualquier sección con roman O título clausular
        candidates: list[Any] = []
        for s in self.sections:
            title = (s.title or "").replace("**", "").strip()
            if not title:
                continue
            # Limpiar placeholder ordinal "N." o "N+1." si lo tiene
            cleaned_title = placeholder_n_re.sub("", title).strip()
            if meta_kw.match(cleaned_title):
                continue
            # Aceptar como cláusula si:
            #   - tenía roman propio detectado, o
            #   - el title (limpio) empieza con ordinal/keyword clausular, o
            #   - originalmente tenía placeholder "N." (se limpió → es cláusula)
            had_n_placeholder = placeholder_n_re.match(title) is not None
            if s.roman or clausular_kw.match(cleaned_title) or had_n_placeholder:
                candidates.append((s, cleaned_title))

        # Fallback: si la heurística filtró demasiado (<3), usar todas
        # las secciones excepto las claramente meta
        if len(candidates) < 3:
            candidates = []
            for s in self.sections:
                title = (s.title or "").replace("**", "").strip()
                cleaned = placeholder_n_re.sub("", title).strip()
                if not cleaned or meta_kw.match(cleaned):
                    continue
                candidates.append((s, cleaned))

        # 2. Asignar romans secuenciales si no hay
        ordinals = [
            "PRIMERA", "SEGUNDA", "TERCERA", "CUARTA", "QUINTA",
            "SEXTA", "SÉPTIMA", "OCTAVA", "NOVENA", "DÉCIMA",
            "UNDÉCIMA", "DUODÉCIMA", "DÉCIMA TERCERA", "DÉCIMA CUARTA",
            "DÉCIMA QUINTA",
        ]

        # Regex amplio para "ya tiene ordinal" — captura PRIMERA, SEGUNDA-N,
        # PENÚLTIMA, DÉCIMA TERCERA, I., II., etc. seguidos de cualquier
        # delimitador (`.`, ` `, `-`, `:`).
        ordinal_prefix_re = _re.compile(
            r"^\s*(PRIMERA|SEGUNDA|TERCERA|CUARTA|QUINTA|SEXTA|S[EÉ]PTIMA|"
            r"OCTAVA|NOVENA|D[EÉ]CIMA(?:\s+(?:PRIMERA|SEGUNDA|TERCERA))?|"
            r"UND[EÉ]CIMA|DUOD[EÉ]CIMA|PEN[UÚ]LTIMA|[UÚ]LTIMA|"
            r"I|II|III|IV|V|VI|VII|VIII|IX|X|XI|XII|XIII)\b"
            r"[\.\-\s:]*([A-Z]\.)?",
            _re.IGNORECASE,
        )
        # Patrón para limpiar "SEGUNDA-N. X" → "X" antes de prefijar
        cleanup_dash_n_re = _re.compile(
            r"^\s*(?:PRIMERA|SEGUNDA|TERCERA|CUARTA|QUINTA|SEXTA|S[EÉ]PTIMA|"
            r"OCTAVA|NOVENA|D[EÉ]CIMA|PEN[UÚ]LTIMA|[UÚ]LTIMA|"
            r"I|II|III|IV|V|VI|VII|VIII|IX|X)\s*-\s*[A-Z]+\.?\s*",
            _re.IGNORECASE,
        )

        out: list[dict[str, Any]] = []
        for i, (s, clean_title) in enumerate(candidates, start=1):
            roman = s.roman or (ordinals[i - 1] if i <= len(ordinals) else f"CL.{i}")
            already_has_ordinal = bool(ordinal_prefix_re.match(clean_title))
            if already_has_ordinal:
                # Normalizar "SEGUNDA-N. Facultades específicas" → "SEGUNDA. Facultades específicas"
                normalized = cleanup_dash_n_re.sub("", clean_title).strip()
                if normalized and not ordinal_prefix_re.match(normalized):
                    # Si el cleanup quitó el ordinal, volverlo a poner
                    final_title = f"{roman}. {normalized}"
                else:
                    final_title = normalized or clean_title
            else:
                final_title = f"{roman}. {clean_title}"
            out.append({
                "key": (s.key or f"clausula_{i}")[:60],
                "title": final_title,
                "order": i,
                "roman": roman,
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
