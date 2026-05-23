"""TemplateDef · Acción de Tutela (Decreto 2591/91)."""
from lex.templates.base import (
    DetailProfile, ForensicStructure, HunterQuery, Rule, SectionDef, TemplateDef,
)

TEMPLATE = TemplateDef(
    id="tutela",
    nombre="Acción de Tutela",
    jurisdiccion="constitucional",
    materia="protección derechos fundamentales",
    description="Acción de Tutela conforme Decreto 2591/1991 + Art. 86 CN",
    sections_plan=[
        SectionDef("encabezado", "Encabezado", 1, roman=None),
        SectionDef("partes", "PARTES", 2, roman="I",
                   expected_blocks=["subsection", "paragraph"]),
        SectionDef("hechos", "HECHOS", 3, roman="II", expected_blocks=["hecho"],
                   section_instruction="Narra 5-10 hechos con fechas precisas."),
        SectionDef("derechos_vulnerados", "DERECHOS FUNDAMENTALES VULNERADOS", 4, roman="III",
                   expected_blocks=["paragraph", "norma_citada"]),
        SectionDef("procedibilidad", "PROCEDIBILIDAD DE LA ACCIÓN", 5, roman="IV",
                   expected_blocks=["paragraph"],
                   section_instruction="Argumenta inmediatez, subsidiariedad y legitimación activa."),
        SectionDef("pretensiones", "PRETENSIONES", 6, roman="V",
                   expected_blocks=["pretension"]),
        SectionDef("fundamentos_derecho", "FUNDAMENTOS DE DERECHO", 7, roman="VI",
                   expected_blocks=["paragraph", "jurisprudencia"],
                   section_instruction="Cita mínimo 2 sentencias Corte Constitucional (T-, C-, SU-) con M.P."),
        SectionDef("pruebas", "PRUEBAS", 8, roman="VII", expected_blocks=["list_item"]),
        SectionDef("juramento", "JURAMENTO (Art. 37 D. 2591/91)", 9, roman=None,
                   expected_blocks=["juramento"]),
        SectionDef("firma", "Firma", 10, roman=None, expected_blocks=["firma"]),
    ],
    hunters=[
        HunterQuery("acción de tutela derecho fundamental procedencia inmediatez subsidiariedad", "corte_constitucional"),
        HunterQuery("Art. 86 Constitución Política tutela mecanismo subsidiario", "corte_constitucional"),
        HunterQuery("Decreto 2591/91 reglamentación acción tutela", "normas_codigos"),
    ],
    validation_rules=[
        Rule.has_section("derechos_vulnerados"),
        Rule.has_section("procedibilidad"),
        Rule.has_section("juramento"),
        Rule.min_pretensiones(2),
    ],
    detail_profile=DetailProfile(
        min_paragraphs_per_section=3, min_hechos=5, min_pretensiones=3,
        require_silogismo=False, require_calculos=False, require_juramento=True,
        require_pretensiones_romanas=False, jurisprudencia_min=2, normas_min=3,
        tone="solemne_constitucional",
    ),
    forensic_structure=ForensicStructure(
        style="tutela", pretensiones_label="PRIMERO",
        show_juramento_section=True,
    ),
    required_data={
        "accionante_nombre": "str", "accionante_cc": "str",
        "accionado_entidad": "str", "derecho_vulnerado": "str",
        "fecha_hecho": "date YYYY-MM-DD", "ciudad": "str",
    },
    calculadora=None,
)
