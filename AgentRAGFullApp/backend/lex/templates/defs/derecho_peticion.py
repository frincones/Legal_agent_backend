"""TemplateDef · Derecho de Petición (Ley 1755/2015)."""
from lex.templates.base import (
    DetailProfile, ForensicStructure, HunterQuery, Rule, SectionDef, TemplateDef,
)

TEMPLATE = TemplateDef(
    id="derecho_peticion",
    nombre="Derecho de Petición",
    jurisdiccion="administrativo",
    materia="petición de información, consulta o queja",
    description="Derecho de Petición conforme Ley 1755/2015 (Art. 23 CN)",
    sections_plan=[
        SectionDef("encabezado", "Encabezado", 1, roman=None),
        SectionDef("peticionario", "PETICIONARIO", 2, roman="I"),
        SectionDef("hechos", "HECHOS", 3, roman="II", expected_blocks=["hecho"]),
        SectionDef("peticion", "PETICIÓN CONCRETA", 4, roman="III",
                   expected_blocks=["paragraph", "list_item"],
                   section_instruction="Enumera puntualmente lo que se solicita. Cada petición en list_item."),
        SectionDef("fundamentos_derecho", "FUNDAMENTOS DE DERECHO", 5, roman="IV",
                   expected_blocks=["paragraph", "norma_citada"]),
        SectionDef("notificaciones", "NOTIFICACIONES", 6, roman="V"),
        SectionDef("firma", "Firma", 7, roman=None, expected_blocks=["firma"]),
    ],
    hunters=[
        HunterQuery("derecho de petición Art. 23 Constitución Ley 1755", "corte_constitucional"),
        HunterQuery("derecho fundamental petición respuesta término 15 días", "corte_constitucional"),
    ],
    validation_rules=[
        Rule.has_section("peticion"),
        Rule.cita_minimo(["Art. 23", "Ley 1755"]),
    ],
    detail_profile=DetailProfile(
        min_paragraphs_per_section=2, min_hechos=3, min_pretensiones=0,
        require_silogismo=False, require_calculos=False, require_juramento=False,
        jurisprudencia_min=1, normas_min=2, tone="petitorio_administrativo",
    ),
    forensic_structure=ForensicStructure(
        style="petición", show_juramento_section=False, pretensiones_label="PRIMERO",
    ),
    required_data={
        "peticionario_nombre": "str", "peticionario_cc": "str",
        "entidad_destinataria": "str", "peticion_concreta": "str", "ciudad": "str",
    },
    calculadora=None,
)
