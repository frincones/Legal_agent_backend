"""Calculadora civil — intereses moratorios, indexación, liquidación ejecutivo."""
from __future__ import annotations

from datetime import date
from typing import Any

from lex.calc.intereses_base import (
    dias_entre,
    indexacion_ipc,
    ipc_anual,
    parse_date,
)


# Tasa máxima legal de intereses moratorios — promedio anual histórico
# Fuente: Superfinanciera (en producción consultar API en tiempo real)
TASA_MORATORIA_ANUAL: dict[int, float] = {
    2020: 26.39,
    2021: 25.50,
    2022: 28.07,
    2023: 36.07,
    2024: 31.27,
    2025: 27.50,
    2026: 25.00,
}


def tasa_moratoria(anio: int) -> float:
    return TASA_MORATORIA_ANUAL.get(anio, TASA_MORATORIA_ANUAL[max(TASA_MORATORIA_ANUAL.keys())])


def intereses_moratorios(
    capital: float,
    fecha_desde: str | date,
    fecha_hasta: str | date,
    tasa_anual: float | None = None,
) -> dict[str, Any]:
    """Intereses moratorios civiles (Art. 1617 CC + Art. 884 CCom).
    Si tasa_anual=None, usa la tasa máxima legal del año de fin."""
    fd = parse_date(fecha_desde)
    fh = parse_date(fecha_hasta)
    dias = max(0, dias_entre(fd, fh))
    tasa = tasa_anual if tasa_anual is not None else tasa_moratoria(fh.year)
    # Interés simple (en práctica forense)
    valor = round(capital * (tasa / 100.0) * (dias / 360.0))
    return {
        "valor": valor,
        "formula": "Intereses moratorios = Capital × Tasa anual × días/360",
        "aplicacion": f"{capital:,.0f} × {tasa:.2f}% × {dias}/360 = {valor:,.0f}",
        "norma_ref": "Art. 884 CCom + Art. 1617 CC",
        "concepto": "Intereses moratorios",
        "dias": dias,
        "tasa_aplicada": tasa,
    }


def intereses_moratorios_indexacion(
    capital: float,
    fecha_desde: str | date,
    fecha_hasta: str | date,
    tasa_anual: float | None = None,
) -> dict[str, Any]:
    """Composite: indexación IPC + intereses moratorios."""
    fd = parse_date(fecha_desde)
    fh = parse_date(fecha_hasta)
    indexado = indexacion_ipc(capital, fd.year, fh.year)
    indem = intereses_moratorios(indexado, fd, fh, tasa_anual)
    return {
        "capital_original": capital,
        "capital_indexado": round(indexado),
        "intereses_moratorios": indem,
        "total": round(indexado) + indem["valor"],
        "norma_ref": "Art. 884 CCom + indexación jurisprudencial",
    }


def liquidacion_ejecutivo(
    capital: float,
    fecha_titulo: str | date,
    fecha_liquidacion: str | date,
    tasa_anual: float | None = None,
) -> dict[str, Any]:
    """Liquidación ejecutivo: capital + intereses moratorios desde título a hoy."""
    intereses = intereses_moratorios(capital, fecha_titulo, fecha_liquidacion, tasa_anual)
    total = capital + intereses["valor"]
    return {
        "parametros": {
            "capital": capital,
            "fecha_titulo": str(parse_date(fecha_titulo)),
            "fecha_liquidacion": str(parse_date(fecha_liquidacion)),
        },
        "capital": {
            "valor": capital,
            "formula": "Capital del título ejecutivo",
            "aplicacion": f"{capital:,.0f}",
            "norma_ref": "Art. 422 CGP",
            "concepto": "Capital",
        },
        "intereses_moratorios": intereses,
        "total": round(total),
    }
