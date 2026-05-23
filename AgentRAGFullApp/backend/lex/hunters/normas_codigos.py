"""Hunter para texto literal de normas / códigos (CST, CGP, CC, CCom, CP, CPP, CPACA)."""
from lex.hunters.base import HunterBase


class NormasCodigosHunter(HunterBase):
    source_filters = [
        "cst", "cgp", "cc", "ccom", "cp", "cpp", "cpaca", "cn91",
        "constitucion", "ley_50_1990", "ley_100_1993", "ley_789_2002",
        "decreto_1072_2015", "funcion_publica", "ley_1755_2015",
        "ley_820_2003", "ley_1098_2006", "ley_1564_2012",
    ]
    doc_type_filters = ["norma", "ley", "decreto", "codigo", "articulo"]
    corte_label = "Norma"
