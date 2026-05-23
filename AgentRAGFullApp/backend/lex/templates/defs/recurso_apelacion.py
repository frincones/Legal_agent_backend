"""TemplateDef · Recurso de Apelación (CGP/CPACA/CPP)."""
from lex.templates.base import (
    DetailProfile, ForensicStructure, HunterQuery, Rule, SectionDef, TemplateDef,
)

TEMPLATE = TemplateDef(
    id="recurso_apelacion",
    nombre="Recurso de Apelación",
    jurisdiccion="general",
    materia="impugnación providencia judicial",
    description="Recurso ordinario de apelación (CGP Art. 320+, CPP Art. 178+)",
    sections_plan=[
        SectionDef("encabezado", "Encabezado", 1, roman=None),
        SectionDef("identificacion_providencia", "PROVIDENCIA IMPUGNADA", 2, roman="I"),
        SectionDef("antecedentes", "ANTECEDENTES PROCESALES", 3, roman="II",
                   expected_blocks=["hecho"]),
        SectionDef("argumentos_impugnacion", "ARGUMENTOS DE IMPUGNACIÓN", 4, roman="III",
                   expected_blocks=["subsection", "paragraph", "silogismo"],
                   section_instruction="Estructura cada punto impugnado con silogismo: norma + interpretación errada + conclusión."),
        SectionDef("normas_y_jurisprudencia", "NORMAS Y JURISPRUDENCIA APLICABLE", 5, roman="IV",
                   expected_blocks=["norma_citada", "jurisprudencia"]),
        SectionDef("pretensiones", "PRETENSIONES DEL RECURSO", 6, roman="V",
                   expected_blocks=["pretension"]),
        SectionDef("notificaciones", "NOTIFICACIONES", 7, roman="VI"),
        SectionDef("firma", "Firma", 8, roman=None, expected_blocks=["firma"]),
    ],
    hunters=[
        HunterQuery("recurso apelación procedencia agravios CGP Art. 320", "sala_civil_csj"),
        HunterQuery("sustentación recurso apelación argumentos fácticos jurídicos", "sala_civil_csj"),
    ],
    validation_rules=[
        Rule.has_section("identificacion_providencia"),
        Rule.has_section("argumentos_impugnacion"),
        Rule.min_pretensiones(1),
    ],
    detail_profile=DetailProfile(
        min_paragraphs_per_section=4, min_hechos=3, min_pretensiones=2,
        require_silogismo=True, require_juramento=False,
        jurisprudencia_min=3, normas_min=3, tone="solemne_forense",
    ),
    forensic_structure=ForensicStructure(style="recurso", show_juramento_section=False),
    required_data={
        "recurrente_nombre": "str", "providencia_referencia": "str",
        "fecha_providencia": "date YYYY-MM-DD", "juzgado_origen": "str",
        "motivos_impugnacion": "str",
    },
    calculadora=None,
)
