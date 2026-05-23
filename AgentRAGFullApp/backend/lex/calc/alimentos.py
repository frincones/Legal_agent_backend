"""Calculadora alimentos — cuota alimentaria orientativa Ley 1098/2006."""
from __future__ import annotations

from typing import Any

from lex.calc.intereses_base import smlmv_anio


def cuota_alimentaria(
    ingresos_alimentante: float,
    numero_hijos: int = 1,
    anio: int = 2026,
    porcentaje_personalizado: float | None = None,
) -> dict[str, Any]:
    """Cuota alimentaria orientativa basada en jurisprudencia ICBF / Sala Familia CSJ.

    Porcentajes orientativos:
    - 1 hijo: 20-25% ingresos
    - 2 hijos: 30-35%
    - 3+ hijos: 40-45%

    Mínimo absoluto: 50% del SMLMV por hijo.
    """
    if porcentaje_personalizado is not None:
        pct = porcentaje_personalizado
    elif numero_hijos == 1:
        pct = 25.0
    elif numero_hijos == 2:
        pct = 33.0
    elif numero_hijos >= 3:
        pct = 42.0
    else:
        pct = 20.0

    cuota_porcentaje = ingresos_alimentante * (pct / 100.0)
    smlmv = smlmv_anio(anio)
    minimo_legal = 0.5 * smlmv * numero_hijos

    cuota_recomendada = max(cuota_porcentaje, minimo_legal)

    return {
        "valor": round(cuota_recomendada),
        "formula": (f"Cuota alimentaria = max({pct}% ingresos, 50% SMLMV × hijos)"),
        "aplicacion": (f"max({ingresos_alimentante:,.0f} × {pct}%, "
                       f"{smlmv:,.0f} × 0.5 × {numero_hijos}) = "
                       f"max({cuota_porcentaje:,.0f}, {minimo_legal:,.0f}) = "
                       f"{cuota_recomendada:,.0f}"),
        "norma_ref": "Art. 24 Ley 1098/2006 + jurisprudencia Sala Familia CSJ",
        "concepto": "Cuota alimentaria mensual",
        "porcentaje_aplicado": pct,
        "minimo_legal": round(minimo_legal),
        "cuota_por_porcentaje": round(cuota_porcentaje),
    }
