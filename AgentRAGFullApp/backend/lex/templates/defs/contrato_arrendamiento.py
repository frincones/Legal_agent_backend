"""TemplateDef · Contrato de Arrendamiento (Ley 820/2003)."""
from lex.templates.base import (
    DetailProfile, ForensicStructure, HunterQuery, Rule, SectionDef, TemplateDef,
)

TEMPLATE = TemplateDef(
    id="contrato_arrendamiento",
    nombre="Contrato de Arrendamiento de Vivienda Urbana",
    jurisdiccion="civil",
    materia="arrendamiento vivienda urbana",
    description="Contrato de Arrendamiento conforme Ley 820/2003",
    sections_plan=[
        SectionDef("encabezado", "Encabezado", 1, roman=None),
        SectionDef("partes", "PARTES", 2, roman="I"),
        SectionDef("objeto", "OBJETO DEL CONTRATO", 3, roman="II"),
        SectionDef("canon", "CANON Y FORMA DE PAGO", 4, roman="III",
                   expected_blocks=["paragraph", "calc_step"]),
        SectionDef("duracion", "DURACIÓN Y PRÓRROGA", 5, roman="IV"),
        SectionDef("obligaciones_arrendador", "OBLIGACIONES DEL ARRENDADOR", 6, roman="V",
                   expected_blocks=["list_item"]),
        SectionDef("obligaciones_arrendatario", "OBLIGACIONES DEL ARRENDATARIO", 7, roman="VI",
                   expected_blocks=["list_item"]),
        SectionDef("garantias", "GARANTÍAS", 8, roman="VII"),
        SectionDef("terminacion", "CAUSALES DE TERMINACIÓN", 9, roman="VIII",
                   expected_blocks=["list_item"]),
        SectionDef("clausulas_especiales", "CLÁUSULAS ESPECIALES", 10, roman="IX"),
        SectionDef("firmas", "Firmas", 11, roman=None, expected_blocks=["firma"]),
    ],
    hunters=[
        HunterQuery("Ley 820/2003 arrendamiento vivienda urbana obligaciones", "normas_codigos"),
        HunterQuery("incremento canon arrendamiento IPC anual Ley 820", "normas_codigos"),
    ],
    validation_rules=[
        Rule.has_section("canon"),
        Rule.has_section("duracion"),
        Rule.has_section("obligaciones_arrendador"),
        Rule.has_section("obligaciones_arrendatario"),
    ],
    detail_profile=DetailProfile(
        min_paragraphs_per_section=3, min_hechos=0, min_pretensiones=0,
        require_silogismo=False, require_calculos=False, require_juramento=False,
        jurisprudencia_min=0, normas_min=2, tone="comercial_formal",
    ),
    forensic_structure=ForensicStructure(
        style="contrato", show_juramento_section=False, show_firma_section=True,
    ),
    required_data={
        "arrendador_nombre": "str", "arrendador_cc": "str",
        "arrendatario_nombre": "str", "arrendatario_cc": "str",
        "inmueble_direccion": "str", "canon_mensual": "number",
        "duracion_meses": "number", "ciudad": "str",
    },
    calculadora=None,
)
