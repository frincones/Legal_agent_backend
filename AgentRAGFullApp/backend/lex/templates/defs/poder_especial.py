"""TemplateDef · Poder Especial (CGP Art. 74)."""
from lex.templates.base import (
    DetailProfile, ForensicStructure, HunterQuery, Rule, SectionDef, TemplateDef,
)

TEMPLATE = TemplateDef(
    id="poder_especial",
    nombre="Poder Especial",
    jurisdiccion="general",
    materia="representación judicial específica",
    description="Poder Especial conferido por escritura privada (Art. 74 CGP)",
    sections_plan=[
        SectionDef("encabezado", "Encabezado", 1, roman=None),
        SectionDef("poderdante", "PODERDANTE", 2, roman="I"),
        SectionDef("apoderado", "APODERADO", 3, roman="II"),
        SectionDef("facultades", "FACULTADES OTORGADAS", 4, roman="III",
                   expected_blocks=["list_item"],
                   section_instruction="Enumera las facultades específicas (transigir, recibir, sustituir, etc)."),
        SectionDef("asunto", "ASUNTO ESPECÍFICO", 5, roman="IV"),
        SectionDef("firma", "Firma del poderdante", 6, roman=None, expected_blocks=["firma"]),
    ],
    hunters=[
        HunterQuery("poder especial CGP Art. 74 facultades sustituir transigir", "normas_codigos"),
    ],
    validation_rules=[
        Rule.has_section("facultades"),
        Rule.has_section("apoderado"),
    ],
    detail_profile=DetailProfile(
        min_paragraphs_per_section=2, require_juramento=False, normas_min=1,
        tone="comercial_formal",
    ),
    forensic_structure=ForensicStructure(style="poder", show_juramento_section=False),
    required_data={
        "poderdante_nombre": "str", "poderdante_cc": "str",
        "apoderado_nombre": "str", "apoderado_tp": "str",
        "asunto": "str", "ciudad": "str",
    },
    calculadora=None,
)
