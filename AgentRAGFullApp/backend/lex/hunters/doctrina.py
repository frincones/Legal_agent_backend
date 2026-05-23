"""Hunter de doctrina general (fallback / búsqueda amplia sin filtros)."""
from lex.hunters.base import HunterBase


class DoctrinaHunter(HunterBase):
    """Sin filtros — busca en todo el corpus."""
    source_filters = []
    doc_type_filters = []
    corte_label = "doctrina/general"
