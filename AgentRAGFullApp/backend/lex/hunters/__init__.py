"""RAG Hunters especializados por jurisdicción / corte.

Cada hunter realiza queries multi-paralelo contra pgvector con filtros
estructurados y re-ranking. Devuelve hits con metadata (M.P., chunk_id, ratio).
"""
from lex.hunters.base import HunterBase, HunterHit, run_hunters
from lex.hunters.sala_laboral_csj import SalaLaboralCsjHunter
from lex.hunters.sala_civil_csj import SalaCivilCsjHunter
from lex.hunters.sala_penal_csj import SalaPenalCsjHunter
from lex.hunters.corte_constitucional import CorteConstitucionalHunter
from lex.hunters.consejo_estado import ConsejoEstadoHunter
from lex.hunters.doctrina import DoctrinaHunter
from lex.hunters.normas_codigos import NormasCodigosHunter

HUNTER_REGISTRY: dict[str, type[HunterBase]] = {
    "sala_laboral_csj": SalaLaboralCsjHunter,
    "sala_civil_csj": SalaCivilCsjHunter,
    "sala_penal_csj": SalaPenalCsjHunter,
    "corte_constitucional": CorteConstitucionalHunter,
    "consejo_estado": ConsejoEstadoHunter,
    "doctrina": DoctrinaHunter,
    "normas_codigos": NormasCodigosHunter,
}


def get_hunter(name: str) -> type[HunterBase]:
    return HUNTER_REGISTRY.get(name, HunterBase)


__all__ = [
    "HunterBase", "HunterHit", "run_hunters", "HUNTER_REGISTRY", "get_hunter",
    "SalaLaboralCsjHunter", "SalaCivilCsjHunter", "SalaPenalCsjHunter",
    "CorteConstitucionalHunter", "ConsejoEstadoHunter",
    "DoctrinaHunter", "NormasCodigosHunter",
]
