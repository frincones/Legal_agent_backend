"""POST /v1/calc/pension · Sprint 4 · S4-07.

Calculadora determinística de pensión colombiana.
Tipos:
  · vejez         (Ley 100/93 Art. 33 mod. Ley 797/2003 Art. 9)
  · invalidez     (Ley 860/2003 Art. 1)
  · sobrevivencia (Ley 797/2003 Art. 12)
  · orfandad      (Ley 100/93 Art. 47)

NO usa LLM. Lee constantes desde `legal_constants` (semanas, edades).

Devuelve elegibilidad + IBL estimado + monto mensual + observaciones.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/calc", tags=["calc"])


TipoPension = Literal["vejez", "invalidez", "sobrevivencia", "orfandad"]
GeneroLit = Literal["mujer", "hombre"]


class PensionRequest(BaseModel):
    tipo: TipoPension
    fecha_nacimiento: Optional[date] = None
    genero: Optional[GeneroLit] = None
    semanas_cotizadas_total: Optional[int] = Field(None, ge=0, le=3000)
    semanas_ultimos_3_anios: Optional[int] = Field(None, ge=0, le=156)
    ibl_promedio_cop: Optional[float] = Field(None, ge=0)
    porcentaje_perdida_capacidad: Optional[float] = Field(None, ge=0, le=100)
    es_estudiante: Optional[bool] = None  # solo orfandad ≥ 18 años


class PensionResponse(BaseModel):
    tipo: str
    elegible: bool
    razon: str
    ibl_cop: Optional[float]
    monto_mensual_cop: Optional[float]
    porcentaje_aplicado: Optional[float]
    requisitos: list[dict]  # cada requisito: {label, exigido, real, cumple}
    fundamento: str
    observaciones: list[str]
    desglose_legible: str


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


async def _load_constants(conn) -> dict:
    rows = await conn.fetch(
        """
        select key, value_numeric
        from legal_constants
        where jurisdiccion in ('CO', 'co')
          and key in (
            'smmlv',
            'pension_vejez_semanas_minimas',
            'pension_vejez_edad_mujer',
            'pension_vejez_edad_hombre',
            'pension_invalidez_semanas_3a',
            'pension_sobrevivencia_semanas_3a'
          )
        """
    )
    out: dict = {}
    for r in rows:
        if r["value_numeric"] is not None:
            out[r["key"]] = float(r["value_numeric"])
    return out


def _calc_age_years(fecha_nac: date, ref: date) -> int:
    years = ref.year - fecha_nac.year
    if (ref.month, ref.day) < (fecha_nac.month, fecha_nac.day):
        years -= 1
    return years


def _percent_vejez(semanas: int) -> float:
    """Tabla simplificada Art. 10 Ley 797/2003.

    Tasa de reemplazo cuando se cumple el mínimo de 1300 semanas:
      · 65% del IBL para 1300 semanas
      · +1.5% por cada 50 semanas adicionales hasta tope de 80%.
    """
    if semanas < 1300:
        return 0.0
    extra = max(0, semanas - 1300) // 50
    pct = 65.0 + 1.5 * extra
    return min(pct, 80.0)


# ────────────────────────────────────────────────────────────────────
# Endpoint
# ────────────────────────────────────────────────────────────────────


@router.post("/pension", response_model=PensionResponse)
async def calc_pension(
    body: PensionRequest,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        c = await _load_constants(conn)

    smmlv = c.get("smmlv", 1620000.0)
    requisitos: list[dict] = []
    obs: list[str] = []

    if body.tipo == "vejez":
        if body.genero is None or body.fecha_nacimiento is None:
            raise HTTPException(400, "vejez requiere genero y fecha_nacimiento")
        edad_min = int(c.get(
            "pension_vejez_edad_mujer" if body.genero == "mujer" else "pension_vejez_edad_hombre",
            57 if body.genero == "mujer" else 62,
        ))
        semanas_min = int(c.get("pension_vejez_semanas_minimas", 1300))
        edad_actual = _calc_age_years(body.fecha_nacimiento, date.today())
        semanas = body.semanas_cotizadas_total or 0
        cumple_edad = edad_actual >= edad_min
        cumple_semanas = semanas >= semanas_min
        requisitos = [
            {"label": f"Edad mínima ({edad_min} años)", "exigido": edad_min,
             "real": edad_actual, "cumple": cumple_edad},
            {"label": f"Semanas cotizadas ({semanas_min})", "exigido": semanas_min,
             "real": semanas, "cumple": cumple_semanas},
        ]
        elegible = cumple_edad and cumple_semanas
        razon = (
            "Cumple ambos requisitos (edad + semanas)."
            if elegible
            else (
                f"Faltan {max(0, edad_min - edad_actual)} año(s) de edad y "
                f"{max(0, semanas_min - semanas)} semana(s) de cotización."
            )
        )
        pct = _percent_vejez(semanas) if elegible else 0.0
        ibl = body.ibl_promedio_cop
        monto = (ibl * pct / 100.0) if (ibl and elegible) else None
        if monto is not None and monto < smmlv:
            monto = smmlv
            obs.append(
                "Mesada ajustada al SMMLV vigente (Art. 35 Ley 100/93 · ninguna "
                "pensión puede ser inferior al salario mínimo)."
            )
        if elegible and ibl is None:
            obs.append("Sin IBL no se puede calcular monto. Provee `ibl_promedio_cop`.")
        return PensionResponse(
            tipo=body.tipo,
            elegible=elegible,
            razon=razon,
            ibl_cop=ibl,
            monto_mensual_cop=monto,
            porcentaje_aplicado=pct if elegible else None,
            requisitos=requisitos,
            fundamento="Ley 100/93 Art. 33 mod. Ley 797/2003 Arts. 9 y 10.",
            observaciones=obs,
            desglose_legible=(
                f"Vejez · {body.genero} · {edad_actual}/{edad_min} años · "
                f"{semanas}/{semanas_min} semanas · "
                f"{'ELEGIBLE' if elegible else 'NO elegible'}"
                + (f" · {pct:.1f}% del IBL = {monto:,.0f} COP" if monto else "")
            ),
        )

    if body.tipo == "invalidez":
        if body.porcentaje_perdida_capacidad is None:
            raise HTTPException(400, "invalidez requiere porcentaje_perdida_capacidad")
        semanas_3a = body.semanas_ultimos_3_anios or 0
        semanas_min_3a = int(c.get("pension_invalidez_semanas_3a", 50))
        cumple_pcl = body.porcentaje_perdida_capacidad >= 50.0
        cumple_semanas = semanas_3a >= semanas_min_3a
        requisitos = [
            {"label": "Pérdida de capacidad laboral ≥ 50%",
             "exigido": 50.0, "real": body.porcentaje_perdida_capacidad,
             "cumple": cumple_pcl},
            {"label": f"Cotización mínima en últimos 3 años ({semanas_min_3a} semanas)",
             "exigido": semanas_min_3a, "real": semanas_3a, "cumple": cumple_semanas},
        ]
        elegible = cumple_pcl and cumple_semanas
        # Tabla simplificada Art. 40 Ley 100:
        #   · PCL 50-66%   → 45% IBL + 1.5% por cada 50 semanas adicionales > 500
        #   · PCL > 66%    → 54% IBL + 2.0% por cada 50 semanas adicionales > 800
        pct = 0.0
        if elegible:
            semanas_total = body.semanas_cotizadas_total or 0
            if body.porcentaje_perdida_capacidad >= 66.0:
                extra = max(0, semanas_total - 800) // 50
                pct = min(54.0 + 2.0 * extra, 75.0)
            else:
                extra = max(0, semanas_total - 500) // 50
                pct = min(45.0 + 1.5 * extra, 75.0)
        ibl = body.ibl_promedio_cop
        monto = (ibl * pct / 100.0) if (ibl and elegible) else None
        if monto is not None and monto < smmlv:
            monto = smmlv
            obs.append("Mesada ajustada al SMMLV vigente.")
        return PensionResponse(
            tipo=body.tipo,
            elegible=elegible,
            razon=(
                "Cumple PCL y semanas requeridas." if elegible
                else "No cumple uno o más requisitos. Revisa la tabla."
            ),
            ibl_cop=ibl,
            monto_mensual_cop=monto,
            porcentaje_aplicado=pct if elegible else None,
            requisitos=requisitos,
            fundamento="Ley 860/2003 Art. 1 + Ley 100/93 Art. 40.",
            observaciones=obs,
            desglose_legible=(
                f"Invalidez · PCL {body.porcentaje_perdida_capacidad:.1f}% · "
                f"{semanas_3a}/{semanas_min_3a} semanas (3a) · "
                f"{'ELEGIBLE' if elegible else 'NO elegible'}"
                + (f" · {pct:.1f}% del IBL = {monto:,.0f} COP" if monto else "")
            ),
        )

    if body.tipo == "sobrevivencia":
        semanas_3a = body.semanas_ultimos_3_anios or 0
        semanas_min_3a = int(c.get("pension_sobrevivencia_semanas_3a", 50))
        cumple = semanas_3a >= semanas_min_3a
        requisitos = [
            {"label": f"Cotización causante en últimos 3 años ({semanas_min_3a} semanas)",
             "exigido": semanas_min_3a, "real": semanas_3a, "cumple": cumple},
        ]
        # Sustitución pensional · 100% de la pensión que recibía o le habría correspondido.
        ibl = body.ibl_promedio_cop
        # Asumimos 65% IBL como base (igual que vejez mínima).
        pct = 65.0 if cumple else 0.0
        monto = (ibl * pct / 100.0) if (ibl and cumple) else None
        if monto is not None and monto < smmlv:
            monto = smmlv
            obs.append("Mesada ajustada al SMMLV vigente.")
        return PensionResponse(
            tipo=body.tipo,
            elegible=cumple,
            razon=(
                "Cumple semanas requeridas." if cumple
                else f"Faltan {max(0, semanas_min_3a - semanas_3a)} semanas en últimos 3 años."
            ),
            ibl_cop=ibl,
            monto_mensual_cop=monto,
            porcentaje_aplicado=pct if cumple else None,
            requisitos=requisitos,
            fundamento="Ley 797/2003 Art. 12 + Ley 100/93 Art. 46.",
            observaciones=obs,
            desglose_legible=(
                f"Sobrevivencia · {semanas_3a}/{semanas_min_3a} semanas (3a) · "
                f"{'ELEGIBLE' if cumple else 'NO elegible'}"
                + (f" · {monto:,.0f} COP/mes" if monto else "")
            ),
        )

    if body.tipo == "orfandad":
        if body.fecha_nacimiento is None:
            raise HTTPException(400, "orfandad requiere fecha_nacimiento del hijo")
        edad = _calc_age_years(body.fecha_nacimiento, date.today())
        if edad < 18:
            elegible = True
            razon = "Hijo menor de 18 años · derecho automático mientras dure la minoría de edad."
        elif edad < 25 and body.es_estudiante:
            elegible = True
            razon = "Hijo entre 18-25 años acreditando estudios."
        else:
            elegible = False
            razon = "No es menor de 18 ni estudiante entre 18-25 (Art. 47 Ley 100/93)."
        requisitos = [
            {"label": "Edad < 18 o (< 25 con estudios)", "exigido": "regla", "real": edad, "cumple": elegible},
        ]
        return PensionResponse(
            tipo=body.tipo,
            elegible=elegible,
            razon=razon,
            ibl_cop=None,
            monto_mensual_cop=None,
            porcentaje_aplicado=None,
            requisitos=requisitos,
            fundamento="Ley 100/93 Art. 47 lit. c · pensión de sobrevivientes (orfandad).",
            observaciones=[
                "Calcula la mesada como sustitución pensional vía cálculo de sobrevivencia.",
            ],
            desglose_legible=f"Orfandad · {edad} años · {'ELEGIBLE' if elegible else 'NO elegible'}",
        )

    raise HTTPException(400, f"tipo desconocido: {body.tipo}")
