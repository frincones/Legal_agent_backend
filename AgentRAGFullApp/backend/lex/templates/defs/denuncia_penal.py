"""TemplateDef · Denuncia Penal (CPP Ley 906/2004)."""
from lex.templates.base import (
    DetailProfile, ForensicStructure, HunterQuery, Rule, SectionDef, TemplateDef,
)

TEMPLATE = TemplateDef(
    id="denuncia_penal",
    nombre="Denuncia Penal",
    jurisdiccion="penal",
    materia="noticia criminal",
    description="Denuncia Penal ante Fiscalía General de la Nación (Art. 67 CPP)",
    sections_plan=[
        SectionDef("encabezado", "Encabezado", 1, roman=None),
        SectionDef("denunciante", "DENUNCIANTE", 2, roman="I"),
        SectionDef("denunciado", "DENUNCIADO O DESCONOCIDO", 3, roman="II"),
        SectionDef("hechos", "HECHOS", 4, roman="III", expected_blocks=["hecho"],
                   section_instruction="Narra hechos en orden cronológico con fecha, hora, lugar y modus operandi."),
        SectionDef("tipificacion", "TIPIFICACIÓN PENAL", 5, roman="IV",
                   expected_blocks=["paragraph", "norma_citada"],
                   section_instruction="Identifica el tipo penal aplicable del CP con artículo + agravantes/atenuantes."),
        SectionDef("dosificacion", "DOSIFICACIÓN PUNITIVA (orientativa)", 6, roman="V",
                   expected_blocks=["calc_step"]),
        SectionDef("pretension", "PETICIÓN", 7, roman="VI", expected_blocks=["pretension"]),
        SectionDef("pruebas", "PRUEBAS", 8, roman="VII", expected_blocks=["list_item"]),
        SectionDef("juramento", "JURAMENTO (Art. 69 CPP)", 9, roman=None, expected_blocks=["juramento"]),
        SectionDef("firma", "Firma", 10, roman=None, expected_blocks=["firma"]),
    ],
    hunters=[
        HunterQuery("Art. 67 CPP noticia criminal denuncia formalidades", "sala_penal_csj"),
        HunterQuery("dosificación punitiva Art. 60 61 CP cuartos movilidad", "sala_penal_csj"),
    ],
    validation_rules=[
        Rule.has_section("hechos"),
        Rule.has_section("tipificacion"),
        Rule.has_section("juramento"),
        Rule.min_hechos(8),
    ],
    detail_profile=DetailProfile(
        min_paragraphs_per_section=3, min_hechos=10, min_pretensiones=1,
        require_silogismo=False, require_calculos=True, require_juramento=True,
        jurisprudencia_min=2, normas_min=3, tone="solemne_forense",
    ),
    forensic_structure=ForensicStructure(style="denuncia", pretensiones_label="PRIMERO"),
    required_data={
        "denunciante_nombre": "str", "denunciante_cc": "str",
        "denunciado_nombre": "str", "delito": "str",
        "fecha_hecho": "date YYYY-MM-DD", "lugar_hecho": "str",
    },
    calculadora="lex.calc.penal:dosificacion_punitiva",
)
