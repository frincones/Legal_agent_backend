"""TemplateDef · Concepto Jurídico (memorial doctrinal)."""
from lex.templates.base import (
    DetailProfile, ForensicStructure, HunterQuery, Rule, SectionDef, TemplateDef,
)

TEMPLATE = TemplateDef(
    id="concepto_juridico",
    nombre="Concepto Jurídico",
    jurisdiccion="general",
    materia="análisis doctrinal",
    description="Concepto jurídico (memorial doctrinal con tesis)",
    sections_plan=[
        SectionDef("encabezado", "Encabezado", 1, roman=None),
        SectionDef("antecedentes", "ANTECEDENTES Y CONTEXTO", 2, roman="I"),
        SectionDef("problema_juridico", "PROBLEMA JURÍDICO", 3, roman="II",
                   section_instruction="Plantea la pregunta jurídica de manera clara y concisa."),
        SectionDef("marco_normativo", "MARCO NORMATIVO Y JURISPRUDENCIAL", 4, roman="III",
                   expected_blocks=["subsection", "paragraph", "norma_citada", "jurisprudencia"]),
        SectionDef("analisis", "ANÁLISIS JURÍDICO", 5, roman="IV",
                   expected_blocks=["subsection", "paragraph", "silogismo"],
                   section_instruction="Desarrolla el análisis en sub-secciones 4.1, 4.2... con razonamiento silogístico."),
        SectionDef("tesis_conclusiones", "TESIS Y CONCLUSIONES", 6, roman="V",
                   expected_blocks=["paragraph", "list_item"]),
        SectionDef("firma", "Firma del concepto", 7, roman=None, expected_blocks=["firma"]),
    ],
    hunters=[
        HunterQuery("concepto jurídico tesis ratio decidendi precedente vinculante", "corte_constitucional", top_k=5),
        HunterQuery("doctrina interpretación normativa constitucional", "doctrina", top_k=4),
    ],
    validation_rules=[
        Rule.has_section("problema_juridico"),
        Rule.has_section("tesis_conclusiones"),
    ],
    detail_profile=DetailProfile(
        min_paragraphs_per_section=4, require_silogismo=True, require_juramento=False,
        jurisprudencia_min=5, normas_min=4, tone="doctrinal",
    ),
    forensic_structure=ForensicStructure(style="concepto", show_juramento_section=False),
    required_data={
        "consultante": "str", "problema_juridico": "str", "fecha_consulta": "date YYYY-MM-DD",
    },
    calculadora=None,
)
