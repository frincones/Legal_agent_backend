"""Calculadora penal — dosificación punitiva CP Art. 60-61."""
from __future__ import annotations

from typing import Any


def dosificacion_punitiva(
    pena_minima_meses: int,
    pena_maxima_meses: int,
    agravantes: int = 0,
    atenuantes: int = 0,
) -> dict[str, Any]:
    """Dosificación punitiva orientativa CP Art. 60 y 61.

    Sistema de cuartos: el ámbito punitivo se divide en 4 cuartos.
    - Sin agravantes y sin atenuantes → cuarto mínimo
    - Solo atenuantes → cuarto mínimo
    - Solo agravantes → cuarto máximo
    - Concurrentes → cuartos medios
    """
    ambito = pena_maxima_meses - pena_minima_meses
    cuarto_size = ambito / 4.0

    if agravantes == 0 and atenuantes == 0:
        # Cuarto mínimo
        lim_inf = pena_minima_meses
        lim_sup = pena_minima_meses + cuarto_size
        cuarto = "mínimo"
    elif agravantes == 0 and atenuantes > 0:
        lim_inf = pena_minima_meses
        lim_sup = pena_minima_meses + cuarto_size
        cuarto = "mínimo"
    elif agravantes > 0 and atenuantes == 0:
        lim_inf = pena_minima_meses + 3 * cuarto_size
        lim_sup = pena_maxima_meses
        cuarto = "máximo"
    else:
        lim_inf = pena_minima_meses + cuarto_size
        lim_sup = pena_minima_meses + 3 * cuarto_size
        cuarto = "medios"

    return {
        "valor_anios_min": round(lim_inf / 12, 2),
        "valor_anios_max": round(lim_sup / 12, 2),
        "formula": "Cuartos punitivos = (pena_máx - pena_mín) / 4, ubicación según agravantes/atenuantes",
        "aplicacion": (f"Ámbito {pena_minima_meses}-{pena_maxima_meses} meses ÷ 4 = "
                       f"{cuarto_size:.1f} meses/cuarto → cuarto {cuarto}: "
                       f"{lim_inf:.1f}-{lim_sup:.1f} meses"),
        "norma_ref": "Art. 60 y 61 CP (Ley 599/2000)",
        "concepto": "Dosificación punitiva",
        "cuarto_aplicable": cuarto,
        "pena_min_meses": round(lim_inf, 1),
        "pena_max_meses": round(lim_sup, 1),
    }
