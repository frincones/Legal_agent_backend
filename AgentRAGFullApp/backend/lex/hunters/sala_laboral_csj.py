"""Hunter especializado en Sala Casación Laboral CSJ."""
from lex.hunters.base import HunterBase


class SalaLaboralCsjHunter(HunterBase):
    source_filters = ["csj-laboral", "csj_laboral", "sala_laboral_csj", "csj"]
    doc_type_filters = ["sentencia", "jurisprudencia"]
    corte_label = "CSJ Sala Laboral"
