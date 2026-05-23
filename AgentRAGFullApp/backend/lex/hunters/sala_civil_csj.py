"""Hunter especializado en Sala Casación Civil CSJ."""
from lex.hunters.base import HunterBase


class SalaCivilCsjHunter(HunterBase):
    source_filters = ["csj-civil", "csj_civil", "sala_civil_csj"]
    doc_type_filters = ["sentencia", "jurisprudencia"]
    corte_label = "CSJ Sala Civil"
