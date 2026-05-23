"""Hunter especializado en Sala Casación Penal CSJ."""
from lex.hunters.base import HunterBase


class SalaPenalCsjHunter(HunterBase):
    source_filters = ["csj-penal", "csj_penal", "sala_penal_csj"]
    doc_type_filters = ["sentencia", "jurisprudencia"]
    corte_label = "CSJ Sala Penal"
