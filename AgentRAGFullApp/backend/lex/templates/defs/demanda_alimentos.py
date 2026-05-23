"""TemplateDef · Demanda de Alimentos (CGP + Ley 1098/06)."""
from lex.templates.base import (
    DetailProfile, ForensicStructure, HunterQuery, Rule, SectionDef, TemplateDef,
)

TEMPLATE = TemplateDef(
    id="demanda_alimentos",
    nombre="Demanda de Alimentos",
    jurisdiccion="familia",
    materia="fijación cuota alimentaria menores de edad",
    description="Demanda de fijación de cuota alimentaria (Ley 1098/2006 + CGP)",
    sections_plan=[
        SectionDef("encabezado", "Encabezado", 1, roman=None),
        SectionDef("partes", "PARTES", 2, roman="I"),
        SectionDef("hechos", "HECHOS", 3, roman="II", expected_blocks=["hecho"]),
        SectionDef("pretensiones", "PRETENSIONES", 4, roman="III",
                   expected_blocks=["pretension"]),
        SectionDef("capacidad_economica", "CAPACIDAD ECONÓMICA DEL ALIMENTANTE", 5, roman="IV",
                   expected_blocks=["paragraph", "calc_step"],
                   section_instruction="Detalla ingresos, gastos y capacidad económica."),
        SectionDef("cuota_solicitada", "CUOTA ALIMENTARIA SOLICITADA", 6, roman="V",
                   expected_blocks=["calc_step", "table"]),
        SectionDef("fundamentos_derecho", "FUNDAMENTOS DE DERECHO", 7, roman="VI",
                   expected_blocks=["paragraph", "norma_citada", "jurisprudencia"]),
        SectionDef("pruebas", "PRUEBAS", 8, roman="VII", expected_blocks=["list_item"]),
        SectionDef("anexos", "ANEXOS", 9, roman="VIII", expected_blocks=["list_item"]),
        SectionDef("notificaciones", "NOTIFICACIONES", 10, roman="IX"),
        SectionDef("juramento", "JURAMENTO", 11, roman=None, expected_blocks=["juramento"]),
        SectionDef("firma", "Firma", 12, roman=None, expected_blocks=["firma"]),
    ],
    hunters=[
        HunterQuery("cuota alimentaria menor de edad Ley 1098 ICBF capacidad económica", "corte_constitucional"),
        HunterQuery("Art. 24 Ley 1098 interés superior menor alimentos", "corte_constitucional"),
        HunterQuery("Sentencia SU Corte Constitucional alimentos progenitor", "corte_constitucional"),
    ],
    validation_rules=[
        Rule.has_section("capacidad_economica"),
        Rule.has_section("cuota_solicitada"),
        Rule.min_pretensiones(3),
    ],
    detail_profile=DetailProfile(
        min_paragraphs_per_section=3, min_hechos=8, min_pretensiones=4,
        require_silogismo=False, require_calculos=True, require_juramento=True,
        require_pretensiones_romanas=True, jurisprudencia_min=2, normas_min=3,
        tone="solemne_forense",
    ),
    forensic_structure=ForensicStructure(style="demanda", pretensiones_label="PRIMERA"),
    required_data={
        "demandante_nombre": "str", "demandado_nombre": "str",
        "menor_nombre": "str", "menor_edad": "number",
        "alimentante_ingresos": "number", "ciudad": "str",
    },
    calculadora="lex.calc.alimentos:cuota_alimentaria",
)
