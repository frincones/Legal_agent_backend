"""TemplateDef catalog + registry para Document Generation v3.1.

DEPRECATED (M19.31 · 29 may 2026)
─────────────────────────────────
La fuente de verdad del estilo y la estructura del documento es ahora
**firm_skills.system_prompt** (SKILL.md), consumido por
`lex.skill_parser` + `lex.orchestrator.stages.skill_loader`.

Cuando el SkillContext está disponible (USE_SKILL_MD_PIPELINE=true, default),
el orchestrator NO usa estos templates Python — solo cae acá como FALLBACK
para doc_types sin SKILL.md.

NO añadir nuevos templates Python aquí. Para añadir un doc_type:
  1. `TEST Freddy/templates_review/XX_<categoria>_<doc_type>_co.md`
  2. Sembrar en firm_skills (migration estilo M19.27)
  3. El doc_type queda disponible sin código Python.
"""
from lex.templates.base import (
    DetailProfile,
    ForensicStructure,
    HunterQuery,
    Rule,
    SectionDef,
    TemplateDef,
)
from lex.templates.registry import registry

__all__ = [
    "DetailProfile",
    "ForensicStructure",
    "HunterQuery",
    "Rule",
    "SectionDef",
    "TemplateDef",
    "registry",
]
