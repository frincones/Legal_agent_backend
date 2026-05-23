"""Hunter especializado en Consejo de Estado (administrativo, tributario)."""
from lex.hunters.base import HunterBase


class ConsejoEstadoHunter(HunterBase):
    source_filters = ["consejo-estado", "consejo_estado", "ce"]
    doc_type_filters = ["sentencia", "jurisprudencia"]
    corte_label = "Consejo de Estado"
