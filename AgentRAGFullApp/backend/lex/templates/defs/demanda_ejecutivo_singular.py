"""TemplateDef · Demanda Ejecutiva Singular (CGP Art. 422+)."""
from lex.templates.base import (
    DetailProfile, ForensicStructure, HunterQuery, Rule, SectionDef, TemplateDef,
)

TEMPLATE = TemplateDef(
    id="demanda_ejecutivo_singular",
    nombre="Demanda Ejecutiva Singular",
    jurisdiccion="civil",
    materia="cobro ejecutivo obligación clara, expresa y exigible",
    description="Demanda ejecutiva singular CGP Art. 422 — título ejecutivo",
    sections_plan=[
        SectionDef("encabezado", "Encabezado", 1, roman=None),
        SectionDef("partes", "PARTES", 2, roman="I"),
        SectionDef("titulo_ejecutivo", "TÍTULO EJECUTIVO", 3, roman="II",
                   expected_blocks=["paragraph"],
                   section_instruction="Describe el título ejecutivo: tipo (pagaré/factura/sentencia), monto, fecha, exigibilidad."),
        SectionDef("hechos", "HECHOS", 4, roman="III", expected_blocks=["hecho"]),
        SectionDef("pretensiones", "PRETENSIONES (MANDAMIENTO DE PAGO)", 5, roman="IV",
                   expected_blocks=["pretension"]),
        SectionDef("liquidacion", "LIQUIDACIÓN DEL CRÉDITO", 6, roman="V",
                   expected_blocks=["calc_step", "table"]),
        SectionDef("fundamentos_derecho", "FUNDAMENTOS DE DERECHO", 7, roman="VI",
                   expected_blocks=["paragraph", "norma_citada"]),
        SectionDef("medidas_cautelares", "MEDIDAS CAUTELARES SOLICITADAS", 8, roman="VII",
                   expected_blocks=["paragraph"]),
        SectionDef("anexos", "ANEXOS", 9, roman="VIII", expected_blocks=["list_item"]),
        SectionDef("notificaciones", "NOTIFICACIONES", 10, roman="IX"),
        SectionDef("juramento", "JURAMENTO", 11, roman=None, expected_blocks=["juramento"]),
        SectionDef("firma", "Firma", 12, roman=None, expected_blocks=["firma"]),
    ],
    hunters=[
        HunterQuery("título ejecutivo pagaré obligación expresa clara exigible Art. 422 CGP", "sala_civil_csj"),
        HunterQuery("intereses moratorios bancarios Superfinanciera tasa máxima", "sala_civil_csj"),
        HunterQuery("medidas cautelares embargo retención fuentes ejecutivo singular CGP", "sala_civil_csj"),
    ],
    validation_rules=[
        Rule.has_section("titulo_ejecutivo"),
        Rule.has_section("liquidacion"),
        Rule.min_pretensiones(2),
    ],
    detail_profile=DetailProfile(
        min_paragraphs_per_section=3, min_hechos=5, min_pretensiones=3,
        require_silogismo=False, require_calculos=True, require_juramento=True,
        require_pretensiones_romanas=True, jurisprudencia_min=1, normas_min=3,
        tone="solemne_forense",
    ),
    forensic_structure=ForensicStructure(style="demanda", pretensiones_label="PRIMERA"),
    required_data={
        "demandante_nombre": "str", "demandado_nombre": "str",
        "titulo_tipo": "str", "monto_capital": "number",
        "fecha_titulo": "date YYYY-MM-DD", "tasa_interes": "number", "ciudad": "str",
    },
    calculadora="lex.calc.civil:liquidacion_ejecutivo",
)
