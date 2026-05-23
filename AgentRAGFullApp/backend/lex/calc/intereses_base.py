"""Series base de IPC, IBC, salario mínimo, UVR Colombia.

Datos hard-coded actualizables anualmente. Para series financieras
en tiempo real, use Banco República API en hot-fetch.
"""
from __future__ import annotations

from datetime import date


# Salario mínimo legal mensual vigente (SMLMV) por año, COP
SMLMV: dict[int, int] = {
    2018: 781_242,
    2019: 828_116,
    2020: 877_803,
    2021: 908_526,
    2022: 1_000_000,
    2023: 1_160_000,
    2024: 1_300_000,
    2025: 1_423_500,
    2026: 1_500_000,  # estimación pendiente decreto
}

# Auxilio de transporte por año (Ley 15/1959), COP
AUXILIO_TRANSPORTE: dict[int, int] = {
    2018: 88_211,
    2019: 97_032,
    2020: 102_854,
    2021: 106_454,
    2022: 117_172,
    2023: 140_606,
    2024: 162_000,
    2025: 200_000,
    2026: 210_000,
}

# IPC anual Colombia (%) — DANE
IPC_ANUAL: dict[int, float] = {
    2018: 3.18,
    2019: 3.80,
    2020: 1.61,
    2021: 5.62,
    2022: 13.12,
    2023: 9.28,
    2024: 5.20,
    2025: 4.30,
    2026: 3.80,  # estimación
}


def smlmv_anio(anio: int) -> int:
    """SMLMV para el año dado. Fallback al último conocido si futuro."""
    if anio in SMLMV:
        return SMLMV[anio]
    last = max(SMLMV.keys())
    return SMLMV[last]


def auxilio_transporte_anio(anio: int) -> int:
    if anio in AUXILIO_TRANSPORTE:
        return AUXILIO_TRANSPORTE[anio]
    last = max(AUXILIO_TRANSPORTE.keys())
    return AUXILIO_TRANSPORTE[last]


def aplica_auxilio_transporte(salario_mensual: int, anio: int) -> bool:
    """Aplica si salario <= 2 SMLMV (Art. 2 Ley 15/1959)."""
    return salario_mensual <= 2 * smlmv_anio(anio)


def ipc_anual(anio: int) -> float:
    if anio in IPC_ANUAL:
        return IPC_ANUAL[anio]
    return IPC_ANUAL[max(IPC_ANUAL.keys())]


def indexacion_ipc(monto: float, anio_origen: int, anio_destino: int) -> float:
    """Indexa un monto entre dos años usando IPC compuesto.
    Indexado = monto × ∏(1 + ipc_anual/100) para cada año entre origen+1 y destino.
    """
    if anio_destino <= anio_origen:
        return monto
    factor = 1.0
    for y in range(anio_origen + 1, anio_destino + 1):
        factor *= 1 + (ipc_anual(y) / 100.0)
    return monto * factor


def dias_entre(d1: date, d2: date) -> int:
    """Diferencia de días entre dos fechas. d2 > d1 retorna positivo."""
    return (d2 - d1).days


def parse_date(s: str | date) -> date:
    """Convierte string ISO (YYYY-MM-DD) a date."""
    if isinstance(s, date):
        return s
    return date.fromisoformat(s)
