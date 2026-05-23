"""Calculadora tributaria — sanciones DIAN, intereses moratorios fiscales."""
from __future__ import annotations

from typing import Any


# Tasa de interés moratorio tributario (modificada anualmente, EA = Efectiva Anual)
TASA_INTERES_MORATORIO_TRIB: dict[int, float] = {
    2023: 36.07,
    2024: 31.27,
    2025: 27.50,
    2026: 25.00,
}


def sancion_por_extemporaneidad(
    impuesto_a_cargo: float,
    meses_retardo: int,
    es_antes_emplazamiento: bool = True,
) -> dict[str, Any]:
    """Sanción por declaración extemporánea (Art. 641 ET).
    - Antes de emplazamiento: 5% por mes (máx 100%)
    - Después: 10% por mes (máx 200%)
    """
    tasa_mensual = 5 if es_antes_emplazamiento else 10
    tope = 100 if es_antes_emplazamiento else 200
    porcentaje = min(meses_retardo * tasa_mensual, tope)
    valor = round(impuesto_a_cargo * (porcentaje / 100.0))
    return {
        "valor": valor,
        "formula": (f"Sanción = Impuesto × min({tasa_mensual}% × meses, {tope}%)"),
        "aplicacion": (f"{impuesto_a_cargo:,.0f} × {porcentaje}% = {valor:,.0f}"),
        "norma_ref": "Art. 641 ET" + (" (antes emplazamiento)" if es_antes_emplazamiento else " (después emplazamiento)"),
        "concepto": "Sanción por extemporaneidad",
    }


def intereses_moratorios_tributarios(
    impuesto_pendiente: float,
    dias_mora: int,
    anio: int = 2026,
) -> dict[str, Any]:
    """Intereses moratorios DIAN (Art. 635 ET)."""
    tasa = TASA_INTERES_MORATORIO_TRIB.get(anio, TASA_INTERES_MORATORIO_TRIB[max(TASA_INTERES_MORATORIO_TRIB.keys())])
    valor = round(impuesto_pendiente * (tasa / 100.0) * (dias_mora / 365.0))
    return {
        "valor": valor,
        "formula": "Intereses = Impuesto × Tasa EA × días/365",
        "aplicacion": f"{impuesto_pendiente:,.0f} × {tasa}% × {dias_mora}/365 = {valor:,.0f}",
        "norma_ref": "Art. 635 ET",
        "concepto": "Intereses moratorios tributarios",
        "tasa_aplicada": tasa,
    }
