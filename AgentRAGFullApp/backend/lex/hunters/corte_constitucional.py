"""Hunter especializado en Corte Constitucional (T, C, SU, A)."""
from lex.hunters.base import HunterBase


class CorteConstitucionalHunter(HunterBase):
    source_filters = ["corte-constitucional", "corte_constitucional", "corte_cc", "cc"]
    doc_type_filters = ["sentencia", "jurisprudencia"]
    corte_label = "Corte Constitucional"
