"""Calculadora laboral pura — CST + Ley 50/1990 + Ley 789/2002.

Funciones puras 100% deterministas. Cero LLM.

Cada función devuelve un dict con:
  - valor: float (COP)
  - formula: str (descripción literal)
  - aplicacion: str (números usados)
  - norma_ref: str (artículo aplicable)
"""
from __future__ import annotations

from datetime import date
from typing import Any

from lex.calc.intereses_base import (
    aplica_auxilio_transporte,
    auxilio_transporte_anio,
    dias_entre,
    parse_date,
    smlmv_anio,
)


def _salario_diario(salario_mensual: float) -> float:
    """Salario base diario: salario / 30 (uso laboral colombiano)."""
    return salario_mensual / 30.0


def _dias_servicios(fecha_ingreso: date, fecha_termino: date) -> int:
    """Días totales de servicio."""
    return max(0, dias_entre(fecha_ingreso, fecha_termino))


def _anios_servicios(fecha_ingreso: date, fecha_termino: date) -> tuple[int, int, int]:
    """Tupla (años, meses, días) para representación humana."""
    total_dias = _dias_servicios(fecha_ingreso, fecha_termino)
    anios = total_dias // 360
    resto = total_dias - (anios * 360)
    meses = resto // 30
    dias = resto - (meses * 30)
    return (anios, meses, dias)


# ============================================================
# CESANTÍAS — Art. 249 CST + Art. 99 Ley 50/1990
# ============================================================

def cesantias(
    salario_mensual: float,
    fecha_ingreso: str | date,
    fecha_termino: str | date,
) -> dict[str, Any]:
    """Cesantías = (salario × días trabajados) / 360.
    Art. 249 CST: 1 mes de salario por cada año de servicios y proporcional.
    """
    fi = parse_date(fecha_ingreso)
    ft = parse_date(fecha_termino)
    dias = _dias_servicios(fi, ft)
    valor = round((salario_mensual * dias) / 360.0)
    return {
        "valor": valor,
        "formula": "Cesantías = (Salario mensual × días trabajados) / 360",
        "aplicacion": f"({salario_mensual:,.0f} × {dias}) / 360 = {valor:,.0f}",
        "norma_ref": "Art. 249 CST + Art. 99 Ley 50/1990",
        "concepto": "Cesantías",
    }


def intereses_cesantias(cesantias_valor: float, dias_periodo: int = 360) -> dict[str, Any]:
    """Intereses sobre cesantías al 12% anual (Art. 1 Ley 52/1975)."""
    valor = round((cesantias_valor * 0.12 * dias_periodo) / 360.0)
    return {
        "valor": valor,
        "formula": "Intereses cesantías = Cesantías × 12% × días/360",
        "aplicacion": f"{cesantias_valor:,.0f} × 0.12 × {dias_periodo}/360 = {valor:,.0f}",
        "norma_ref": "Art. 1 Ley 52/1975 + Art. 99 num. 3 Ley 50/1990",
        "concepto": "Intereses sobre cesantías",
    }


# ============================================================
# INDEMNIZACIÓN ART. 64 CST (modificado por Art. 28 Ley 789/2002)
# ============================================================

def indemnizacion_art_64(
    salario_mensual: float,
    fecha_ingreso: str | date,
    fecha_termino: str | date,
) -> dict[str, Any]:
    """Indemnización por terminación unilateral sin justa causa de contrato a término indefinido.

    Para salario < 10 SMLMV:
      - 30 días salario por el 1er año
      - 20 días adicionales por cada año siguiente
      - proporcional por fracción
    Para salario >= 10 SMLMV:
      - 20 días salario por el 1er año
      - 15 días adicionales por cada año siguiente
    """
    fi = parse_date(fecha_ingreso)
    ft = parse_date(fecha_termino)
    salario_diario = _salario_diario(salario_mensual)
    anios_completos, meses, dias_resto = _anios_servicios(fi, ft)

    anio_termino = ft.year
    es_alto = salario_mensual >= 10 * smlmv_anio(anio_termino)

    if es_alto:
        dias_primer = 20
        dias_adicional_por_anio = 15
        tipo = "alto (>=10 SMLMV)"
    else:
        dias_primer = 30
        dias_adicional_por_anio = 20
        tipo = "bajo (<10 SMLMV)"

    if anios_completos == 0:
        # Solo proporcional sobre los primeros 30/20 días del 1er año
        dias_fraccion_year = meses * 30 + dias_resto
        valor_dias = (dias_primer / 360.0) * dias_fraccion_year
    else:
        # 1er año + adicionales por años subsiguientes + fracción proporcional
        valor_dias = dias_primer + (dias_adicional_por_anio * (anios_completos - 1))
        # Fracción proporcional sobre años adicionales
        fraccion_dias = meses * 30 + dias_resto
        valor_dias += (dias_adicional_por_anio / 360.0) * fraccion_dias

    valor = round(salario_diario * valor_dias)
    return {
        "valor": valor,
        "formula": (f"Indemnización ({tipo}) = (Salario/30) × "
                    f"[{dias_primer} días 1er año + "
                    f"{dias_adicional_por_anio} días × (años-1) + fracción]"),
        "aplicacion": (f"({salario_mensual:,.0f}/30) × {valor_dias:.2f} días = "
                       f"{salario_diario:,.2f} × {valor_dias:.2f} = {valor:,.0f}"),
        "norma_ref": "Art. 64 CST modificado por Art. 28 Ley 789/2002",
        "concepto": "Indemnización por despido sin justa causa",
        "anios_completos": anios_completos,
        "dias_indemnizables": round(valor_dias, 2),
    }


# ============================================================
# SANCIÓN MORATORIA — Art. 65 CST (mod. Art. 29 Ley 789/2002)
# ============================================================

def sancion_moratoria_art_65(
    salario_mensual: float,
    dias_mora: int,
) -> dict[str, Any]:
    """Equivale a 1 día de salario por cada día de mora, máximo 24 meses (720 días).
    Después intereses moratorios a tasa máxima legal certificada por Superfinanciera.
    """
    salario_diario = _salario_diario(salario_mensual)
    dias_efectivos = min(dias_mora, 720)
    valor = round(salario_diario * dias_efectivos)
    nota_extra = (" Después del mes 24 corresponden intereses moratorios a la "
                  "tasa máxima legal certificada por la Superfinanciera.") if dias_mora > 720 else ""
    return {
        "valor": valor,
        "formula": "Sanción moratoria = Salario diario × días de mora (tope 720 días)",
        "aplicacion": f"({salario_mensual:,.0f}/30) × {dias_efectivos} días = {valor:,.0f}{nota_extra}",
        "norma_ref": "Art. 65 CST modificado por Art. 29 Ley 789/2002",
        "concepto": "Sanción moratoria",
        "dias_efectivos": dias_efectivos,
        "dias_totales_mora": dias_mora,
    }


# ============================================================
# VACACIONES — Art. 186 CST
# ============================================================

def vacaciones(
    salario_mensual: float,
    fecha_ingreso: str | date,
    fecha_termino: str | date,
) -> dict[str, Any]:
    """15 días hábiles por año de servicios. Proporcional por fracción.
    Compensables en dinero a la terminación (Art. 189 CST)."""
    fi = parse_date(fecha_ingreso)
    ft = parse_date(fecha_termino)
    dias_servicio = _dias_servicios(fi, ft)
    # 15 días por 360 días trabajados → factor diario 15/360
    dias_vacaciones = round((dias_servicio / 360.0) * 15, 2)
    salario_diario = _salario_diario(salario_mensual)
    valor = round(salario_diario * dias_vacaciones)
    return {
        "valor": valor,
        "formula": "Vacaciones = (Salario/30) × (días_servicio × 15/360)",
        "aplicacion": (f"{salario_diario:,.2f} × {dias_vacaciones:.2f} días = "
                       f"{valor:,.0f}"),
        "norma_ref": "Art. 186 y 189 CST",
        "concepto": "Vacaciones compensadas",
        "dias_vacaciones": dias_vacaciones,
    }


# ============================================================
# PRIMA DE SERVICIOS — Art. 306 CST (mod. Ley 1788/2016)
# ============================================================

def prima_servicios(
    salario_mensual: float,
    fecha_ingreso: str | date,
    fecha_termino: str | date,
) -> dict[str, Any]:
    """30 días salario por año, pagadero por semestres calendario.
    Aplica también al servicio doméstico (Ley 1788/2016).
    """
    fi = parse_date(fecha_ingreso)
    ft = parse_date(fecha_termino)
    dias = _dias_servicios(fi, ft)
    # Aproximación: 30 días salario / 360 × días_trabajados
    valor = round((salario_mensual * dias) / 360.0)
    return {
        "valor": valor,
        "formula": "Prima de servicios = (Salario mensual × días trabajados) / 360",
        "aplicacion": f"({salario_mensual:,.0f} × {dias}) / 360 = {valor:,.0f}",
        "norma_ref": "Art. 306 CST modificado por Art. 1 Ley 1788/2016",
        "concepto": "Prima de servicios",
    }


# ============================================================
# FULL LIQUIDACIÓN — combina todas las prestaciones
# ============================================================

def full_liquidacion(
    salario_mensual: float,
    fecha_ingreso: str | date,
    fecha_termino: str | date,
    dias_mora: int = 0,
    incluir_indemnizacion: bool = True,
) -> dict[str, Any]:
    """Liquidación completa de todas las prestaciones laborales."""
    fi = parse_date(fecha_ingreso)
    ft = parse_date(fecha_termino)
    anios, meses, dias = _anios_servicios(fi, ft)

    cesant = cesantias(salario_mensual, fi, ft)
    int_ces = intereses_cesantias(cesant["valor"])
    vac = vacaciones(salario_mensual, fi, ft)
    prim = prima_servicios(salario_mensual, fi, ft)

    conceptos: dict[str, Any] = {
        "cesantias": cesant,
        "intereses_cesantias": int_ces,
        "vacaciones": vac,
        "prima_servicios": prim,
    }

    if incluir_indemnizacion:
        indem = indemnizacion_art_64(salario_mensual, fi, ft)
        conceptos["indemnizacion_art_64"] = indem

    if dias_mora > 0:
        sancion = sancion_moratoria_art_65(salario_mensual, dias_mora)
        conceptos["sancion_moratoria_art_65"] = sancion

    total = sum(c["valor"] for c in conceptos.values())

    return {
        "parametros": {
            "salario_mensual": salario_mensual,
            "fecha_ingreso": str(fi),
            "fecha_termino": str(ft),
            "dias_servicio": _dias_servicios(fi, ft),
            "tiempo_servido": f"{anios} años, {meses} meses, {dias} días",
            "salario_diario": _salario_diario(salario_mensual),
            "anio_termino": ft.year,
            "smlmv_termino": smlmv_anio(ft.year),
            "aplica_auxilio_transporte": aplica_auxilio_transporte(salario_mensual, ft.year),
        },
        "conceptos": conceptos,
        "total": total,
    }
