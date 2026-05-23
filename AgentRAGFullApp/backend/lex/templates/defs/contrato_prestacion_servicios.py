"""TemplateDef · Contrato de Prestación de Servicios (Civil/Comercial)."""
from lex.templates.base import (
    DetailProfile, ForensicStructure, HunterQuery, Rule, SectionDef, TemplateDef,
)

TEMPLATE = TemplateDef(
    id="contrato_prestacion_servicios",
    nombre="Contrato de Prestación de Servicios",
    jurisdiccion="comercial",
    materia="prestación servicios profesionales independiente",
    description="Contrato de Prestación de Servicios (Art. 2053 CC + Ley 80/93 si público)",
    sections_plan=[
        SectionDef("encabezado", "Encabezado", 1, roman=None),
        SectionDef("partes", "PARTES", 2, roman="I"),
        SectionDef("objeto", "OBJETO Y ALCANCE", 3, roman="II"),
        SectionDef("obligaciones_contratista", "OBLIGACIONES DEL CONTRATISTA", 4, roman="III",
                   expected_blocks=["list_item"]),
        SectionDef("obligaciones_contratante", "OBLIGACIONES DEL CONTRATANTE", 5, roman="IV",
                   expected_blocks=["list_item"]),
        SectionDef("valor_forma_pago", "VALOR Y FORMA DE PAGO", 6, roman="V"),
        SectionDef("duracion", "DURACIÓN", 7, roman="VI"),
        SectionDef("propiedad_intelectual", "PROPIEDAD INTELECTUAL Y CONFIDENCIALIDAD", 8, roman="VII"),
        SectionDef("indemnidad", "CLÁUSULA DE INDEMNIDAD", 9, roman="VIII"),
        SectionDef("terminacion", "CAUSALES DE TERMINACIÓN", 10, roman="IX", expected_blocks=["list_item"]),
        SectionDef("solucion_controversias", "SOLUCIÓN DE CONTROVERSIAS", 11, roman="X"),
        SectionDef("firmas", "Firmas", 12, roman=None, expected_blocks=["firma"]),
    ],
    hunters=[
        HunterQuery("contrato prestación servicios independiente subordinación distinción laboral", "sala_laboral_csj"),
        HunterQuery("cláusula penal indemnización contractual Art. 1592 CC", "sala_civil_csj"),
    ],
    validation_rules=[
        Rule.has_section("objeto"),
        Rule.has_section("valor_forma_pago"),
        Rule.has_section("obligaciones_contratista"),
    ],
    detail_profile=DetailProfile(
        min_paragraphs_per_section=3, require_juramento=False,
        jurisprudencia_min=1, normas_min=2, tone="comercial_formal",
    ),
    forensic_structure=ForensicStructure(style="contrato", show_juramento_section=False),
    required_data={
        "contratante_nombre": "str", "contratante_nit_cc": "str",
        "contratista_nombre": "str", "contratista_nit_cc": "str",
        "objeto_servicio": "str", "valor_total": "number", "duracion_meses": "number",
    },
    calculadora=None,
)
