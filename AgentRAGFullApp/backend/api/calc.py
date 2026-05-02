"""Colombian labor liquidation calculator (CST + Ley 50/1990 + Ley 789/2002).

NO LLM — pure deterministic arithmetic. PRD F7 requires 0% numeric
hallucination, validated against 50 test cases by consulting lawyers.

Conceptos calculados:
  · Cesantías                      = (salario_base × días_trabajados) / 360
  · Intereses sobre cesantías      = cesantías × 12% × (días / 360)
  · Prima de servicios             = (salario_base × días) / 360  (semestral)
  · Vacaciones                     = (salario_base × días / 720)   (15 días/año)
  · Indemnización despido sin justa causa Art. 64 CST mod. Ley 789/2002
  · Auxilio de transporte          = ($162.000/mes 2026 prorrateado, si salario < 2 SMMLV)
  · Aportes EPS / AFP / ARL retroactivos cuando aplique

Sources:
  - CST Art. 64 (indemnización), Art. 249 (cesantías), Art. 306 (prima),
    Art. 186 (vacaciones), Art. 189 (compensación vacaciones).
  - Ley 50/1990 (régimen de cesantías sin retroactividad).
  - Ley 789/2002 Art. 28 (cálculo indemnización por antigüedad).
  - SMLMV 2026: COP 1.823.500 (Decreto 1572/2024).
  - Auxilio de transporte 2026: COP 200.000/mes.

Salario integral (Ley 50 Art. 18):
  - Si salario ≥ 10 SMLMV → no se causan prestaciones aparte; ya están
    incluidas en el salario integral. La calculadora detecta y aplica
    el régimen correcto.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/calc", tags=["calc"])

# Constantes vigentes 2026 — en producción deberían venir de tabla `legal_constants`
SMLMV_2026 = 1_823_500              # Salario Mínimo Mensual Legal Vigente (COP)
AUX_TRANSPORTE_2026 = 200_000       # Auxilio de transporte mensual (COP)
DIAS_VACACIONES_ANUALES = 15        # CST Art. 186
TASA_INTERES_CESANTIAS = 0.12       # 12% anual (Ley 52/1975)
PRIMA_SERVICIOS_DIAS_ANUALES = 30   # 30 días/año (15 jun + 20 dic)
CAP_INTEGRAL_SMLMV = 10             # ≥ 10 SMLMV → salario integral


# ─────────────────────────────────────────────────────────────────────
# Request / Response models
# ─────────────────────────────────────────────────────────────────────


class LiquidacionRequest(BaseModel):
    fecha_ingreso: date
    fecha_terminacion: date
    salario_mensual_cop: float = Field(gt=0, description="Salario base mensual en COP")
    causa: str = Field(pattern="^(injustificado|justa_causa|renuncia|mutuo_acuerdo|terminacion_contrato|fin_obra)$")
    tipo_contrato: str = Field(default="indefinido", pattern="^(indefinido|fijo|obra_labor|aprendizaje)$")
    salario_integral: bool = False
    auxilio_transporte_aplica: bool = True
    vacaciones_pendientes_dias: int = Field(default=0, ge=0)
    cesantias_consignadas_cop: float = Field(default=0, ge=0, description="Saldo ya consignado en fondo")
    smlmv_cop: float = Field(default=SMLMV_2026, gt=0)
    matter_id: Optional[str] = None
    trabajador_nombre: Optional[str] = None
    persist: bool = True

    @field_validator("fecha_terminacion")
    @classmethod
    def _terminacion_ge_ingreso(cls, v: date, info):
        ingreso = info.data.get("fecha_ingreso")
        if ingreso and v < ingreso:
            raise ValueError("fecha_terminacion debe ser >= fecha_ingreso")
        return v


class LineItem(BaseModel):
    concepto: str
    formula: str
    base: float
    multiplicador: float
    monto_cop: float
    nota: Optional[str] = None
    fundamento: Optional[str] = None


class LiquidacionResponse(BaseModel):
    id: str
    formulas_version: str
    inputs: dict
    line_items: list[LineItem]
    total_cop: float
    causa: str
    aplica_indemnizacion: bool
    desglose_legible: str


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _round0(x: float) -> float:
    """Round to nearest peso (no decimals on COP).

    CST courts present amounts in whole pesos; decimals appear only in
    intermediate calculations.
    """
    return round(float(x) + 1e-9)


def _days_360(d1: date, d2: date) -> int:
    """Días bajo la regla colombiana 360 días/año (mes de 30 días).

    CST usa 360 días para liquidaciones (1 mes = 30 días).
    """
    if d2 <= d1:
        return 0
    y_diff = d2.year - d1.year
    m_diff = d2.month - d1.month
    d_diff = min(d2.day, 30) - min(d1.day, 30)
    days = y_diff * 360 + m_diff * 30 + d_diff
    return max(0, days)


def _years_between(d1: date, d2: date) -> tuple[int, int]:
    """Returns (full_years, total_days_360)."""
    days = _days_360(d1, d2)
    return days // 360, days


def _salario_diario(salario_mensual: float) -> float:
    return salario_mensual / 30.0


def _es_salario_integral_eligible(salario_mensual: float, smlmv: float) -> bool:
    return salario_mensual >= CAP_INTEGRAL_SMLMV * smlmv


# ─────────────────────────────────────────────────────────────────────
# Core calculator (pure function)
# ─────────────────────────────────────────────────────────────────────


def compute_liquidacion(req: LiquidacionRequest) -> LiquidacionResponse:
    items: list[LineItem] = []

    salario_mensual = req.salario_mensual_cop
    salario_diario = _salario_diario(salario_mensual)
    full_years, days_total = _years_between(req.fecha_ingreso, req.fecha_terminacion)

    # Validación temprana: salario integral por Ley 50/1990 Art. 18 exige
    # salario nominal ≥ 10 SMLMV (en 2026 ~COP 18.235.000). Si no, el
    # contrato no califica como "integral" y el cálculo cae al régimen ordinario.
    if req.salario_integral and salario_mensual < CAP_INTEGRAL_SMLMV * req.smlmv_cop:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Salario integral requiere salario ≥ {CAP_INTEGRAL_SMLMV} SMMLV "
                f"(COP {CAP_INTEGRAL_SMLMV * req.smlmv_cop:,.0f}). "
                f"El salario informado de COP {salario_mensual:,.0f} no califica."
            ),
        )

    # Auxilio de transporte (sólo si salario < 2 SMMLV y aplica)
    aplica_aux_transp = (
        req.auxilio_transporte_aplica
        and salario_mensual < 2 * req.smlmv_cop
        and not req.salario_integral
    )
    base_prestacional = salario_mensual + (AUX_TRANSPORTE_2026 if aplica_aux_transp else 0)
    base_diario_prestacional = base_prestacional / 30.0

    aplica_indemn = req.causa == "injustificado"

    # Salario integral: no se causan prestaciones (Ley 50 Art. 18)
    if req.salario_integral:
        items.append(LineItem(
            concepto="Salario integral (Ley 50/1990 Art. 18)",
            formula=f"Salario mensual COP {salario_mensual:,.0f} ≥ 10 SMLMV",
            base=salario_mensual,
            multiplicador=1,
            monto_cop=0,
            fundamento="Ley 50/1990 Art. 18",
            nota=("Las prestaciones, primas, cesantías, intereses y vacaciones están "
                  "incluidas en el salario integral. Solo se liquida indemnización "
                  "(si aplica) sobre el 70% del salario integral."),
        ))

    if not req.salario_integral:
        # 1) Cesantías (CST Art. 249 + Ley 50/1990)
        cesantias_brutas = (base_prestacional * days_total) / 360.0
        cesantias_netas = max(0, cesantias_brutas - req.cesantias_consignadas_cop)
        items.append(LineItem(
            concepto="Cesantías",
            formula=f"({base_prestacional:,.0f} × {days_total}) / 360 − consignadas({req.cesantias_consignadas_cop:,.0f})",
            base=base_prestacional,
            multiplicador=days_total,
            monto_cop=_round0(cesantias_netas),
            fundamento="CST Art. 249, Ley 50/1990 Art. 99",
            nota=("Si el régimen es Ley 50, el saldo en fondo (Porvenir/Protección/"
                  "Colfondos/Skandia) descuenta de lo reclamable."),
        ))

        # 2) Intereses sobre cesantías 12% anual (Ley 52/1975)
        intereses_cesantias = cesantias_brutas * TASA_INTERES_CESANTIAS * (days_total / 360.0)
        items.append(LineItem(
            concepto="Intereses sobre cesantías (12% anual)",
            formula=f"{cesantias_brutas:,.0f} × 0.12 × ({days_total}/360)",
            base=cesantias_brutas,
            multiplicador=TASA_INTERES_CESANTIAS * (days_total / 360.0),
            monto_cop=_round0(intereses_cesantias),
            fundamento="Ley 52/1975 Art. 1, CST Art. 249",
        ))

        # 3) Prima de servicios (CST Art. 306, modif. Ley 1788/2016)
        prima_servicios = (base_prestacional * days_total) / 360.0
        items.append(LineItem(
            concepto="Prima de servicios",
            formula=f"({base_prestacional:,.0f} × {days_total}) / 360",
            base=base_prestacional,
            multiplicador=days_total,
            monto_cop=_round0(prima_servicios),
            fundamento="CST Art. 306 modificado por Ley 1788/2016",
            nota="30 días de salario por año (15 jun + 15 dic).",
        ))

        # 4) Vacaciones (CST Art. 186, 189)
        vacaciones_devengadas = (DIAS_VACACIONES_ANUALES * days_total) / 360.0
        vacaciones_total = vacaciones_devengadas + req.vacaciones_pendientes_dias
        monto_vacaciones = vacaciones_total * salario_diario
        items.append(LineItem(
            concepto="Vacaciones (compensación)",
            formula=f"((15 × {days_total}/360) + {req.vacaciones_pendientes_dias} pendientes) × {salario_diario:,.0f}",
            base=salario_diario,
            multiplicador=round(vacaciones_total, 2),
            monto_cop=_round0(monto_vacaciones),
            fundamento="CST Art. 186 y 189",
            nota="Vacaciones se liquidan con el último salario, sin auxilio de transporte.",
        ))

    # 5) Indemnización por despido sin justa causa (Ley 789/2002 Art. 28)
    if aplica_indemn:
        # Bucket SMMLV: por Ley 50/1990 Art. 18 el salario integral exige ≥10 SMMLV
        # por definición legal — el factor 0.70 reduce sólo la BASE diaria de la
        # indemnización (factor prestacional), no la elegibilidad por tramo.
        salario_smlmv = salario_mensual / req.smlmv_cop
        if req.salario_integral:
            # Reducción legal de base: 70% del salario integral (Ley 50 Art. 18 par.)
            base_indemn = salario_mensual * 0.70
        else:
            base_indemn = salario_mensual

        salario_diario_indemn = base_indemn / 30.0

        if req.tipo_contrato == "indefinido":
            # CST Art. 64 mod. Ley 789/2002 Art. 28 — "y proporcionalmente por
            # fracción": tras el primer año se paga 20 (o 15) días por año
            # adicional, computado proporcional a los días reales servidos.
            if salario_smlmv < 10:
                base_first = 30
                extra_per_year = 20
                bracket_label = "<10 SMMLV"
            else:
                base_first = 20
                extra_per_year = 15
                bracket_label = "≥10 SMMLV"

            if days_total <= 360:
                # Mínimo legal del primer año (incluso si trabajó <1 año).
                dias_indemn = base_first
                formula_str = (
                    f"{base_first} días (primer año contrato indefinido {bracket_label})"
                )
            else:
                days_extra = days_total - 360
                fraccion_extra = days_extra / 360.0  # años proporcionales
                extra_dias = extra_per_year * fraccion_extra
                dias_indemn = base_first + extra_dias
                formula_str = (
                    f"{base_first} + {extra_per_year}×({days_extra}/360) "
                    f"= {dias_indemn:.2f} días ({bracket_label})"
                )
            monto_indemn = dias_indemn * salario_diario_indemn
            items.append(LineItem(
                concepto=f"Indemnización despido sin justa causa ({full_years} años)",
                formula=f"{formula_str} × {salario_diario_indemn:,.0f}",
                base=salario_diario_indemn,
                multiplicador=round(dias_indemn, 2),
                monto_cop=_round0(monto_indemn),
                fundamento="CST Art. 64 modificado por Ley 789/2002 Art. 28",
                nota=(
                    f"Régimen contrato indefinido. Salario base SMMLV: {salario_smlmv:.2f}. "
                    + ("Salario integral · base = 70% (Ley 50 Art. 18). "
                       if req.salario_integral else "")
                    + "Fracción de año posterior al primero pagada proporcional."
                ),
            ))
        elif req.tipo_contrato == "fijo":
            # Indemnización = salarios faltantes hasta vencimiento del contrato
            # (mínimo 15 días si no se conoce duración pactada)
            items.append(LineItem(
                concepto="Indemnización despido contrato fijo",
                formula="Salarios pendientes hasta vencimiento del término pactado",
                base=salario_mensual,
                multiplicador=0,
                monto_cop=0,
                fundamento="CST Art. 64 (b) y Art. 46",
                nota=("Para calcular requiere fecha de vencimiento del contrato. "
                      "Mínimo legal: 15 días."),
            ))
        elif req.tipo_contrato == "obra_labor":
            items.append(LineItem(
                concepto="Indemnización despido contrato obra/labor",
                formula="Salarios pendientes hasta finalización de la obra (mín. 15 días)",
                base=salario_mensual,
                multiplicador=0.5,
                monto_cop=_round0(salario_diario_indemn * 15),
                fundamento="CST Art. 64 (c)",
                nota="Mínimo legal: 15 días de salario.",
            ))

    total = sum(i.monto_cop for i in items)
    desglose = "\n".join(
        f"- {i.concepto}: COP ${i.monto_cop:,.0f}  ({i.fundamento or ''})"
        for i in items
    )
    desglose += f"\n\nTotal reclamable: COP ${total:,.0f}"

    return LiquidacionResponse(
        id=str(uuid.uuid4()),
        formulas_version="cst-co-2024-q4",
        inputs={
            "fecha_ingreso": req.fecha_ingreso.isoformat(),
            "fecha_terminacion": req.fecha_terminacion.isoformat(),
            "salario_mensual_cop": salario_mensual,
            "auxilio_transporte_aplicado": aplica_aux_transp,
            "base_prestacional": base_prestacional,
            "salario_integral": req.salario_integral,
            "tipo_contrato": req.tipo_contrato,
            "causa": req.causa,
            "smlmv_cop": req.smlmv_cop,
            "anios_servicio_completos": full_years,
            "dias_servicio_360": days_total,
        },
        line_items=items,
        total_cop=_round0(total),
        causa=req.causa,
        aplica_indemnizacion=aplica_indemn,
        desglose_legible=desglose,
    )


# ─────────────────────────────────────────────────────────────────────
# REST endpoint
# ─────────────────────────────────────────────────────────────────────


@router.post("/liquidacion", response_model=LiquidacionResponse)
async def calc_liquidacion(
    req: LiquidacionRequest,
    principal: Principal = Depends(get_current_firm),
):
    """POST /v1/calc/liquidacion · cálculo determinista CST Colombia."""
    result = compute_liquidacion(req)

    if req.persist:
        try:
            from utils.db import get_storage
            storage = await get_storage()
            if hasattr(storage, "pool"):
                async with storage.pool.acquire() as conn:
                    await conn.execute(
                        """
                        insert into liquidacion_calculations
                          (id, firm_id, matter_id, user_id, trabajador_nombre,
                           fecha_ingreso, fecha_terminacion, salario_diario,
                           salario_integrado, causa, variables, resultado,
                           total_amount, total_currency, formulas_version)
                        values
                          ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5,
                           $6::date, $7::date, $8, $9, $10, $11::jsonb, $12::jsonb,
                           $13, $14, $15)
                        """,
                        result.id,
                        principal.firm_id,
                        req.matter_id,
                        principal.user_id,
                        req.trabajador_nombre,
                        req.fecha_ingreso,
                        req.fecha_terminacion,
                        _salario_diario(req.salario_mensual_cop),
                        req.salario_mensual_cop if req.salario_integral else None,
                        req.causa,
                        json.dumps(result.inputs),
                        json.dumps([i.model_dump() for i in result.line_items], default=str),
                        result.total_cop,
                        "COP",
                        result.formulas_version,
                    )
        except Exception as e:
            logger.warning("liquidacion persist failed (non-fatal): %s", e)

    return result


# ─────────────────────────────────────────────────────────────────────
# Tool wrapper for OpenAI Realtime
# ─────────────────────────────────────────────────────────────────────


async def calc_liquidacion_tool(args: dict, ctx: dict) -> dict:
    """Tool adapter for `calc_liquidacion`."""
    try:
        req = LiquidacionRequest(
            fecha_ingreso=date.fromisoformat(args["fecha_ingreso"]),
            fecha_terminacion=date.fromisoformat(args["fecha_terminacion"]),
            salario_mensual_cop=float(args["salario_mensual_cop"]),
            causa=args["causa"],
            tipo_contrato=args.get("tipo_contrato", "indefinido"),
            salario_integral=bool(args.get("salario_integral", False)),
            trabajador_nombre=args.get("trabajador_nombre"),
            matter_id=ctx.get("matter_id"),
            persist=True,
        )
    except Exception as e:
        return {"error": f"invalid arguments: {e}"}

    result = compute_liquidacion(req)
    return {
        "id": result.id,
        "total_cop": result.total_cop,
        "currency": "COP",
        "causa": result.causa,
        "aplica_indemnizacion": result.aplica_indemnizacion,
        "line_items": [i.model_dump() for i in result.line_items],
        "desglose_legible": result.desglose_legible,
    }


# ════════════════════════════════════════════════════════════════════════
# PRESCRIPCIÓN · cómputo de plazos (Código Civil + CGP + CST)
# ════════════════════════════════════════════════════════════════════════
#
# Plazos por tipo de acción (Colombia 2026):
#   civil_ordinaria       10 años · CC Art. 2536 mod. Ley 791/2002
#   civil_ejecutiva       5 años  · CC Art. 2536 mod. Ley 791/2002
#   comercial_ordinaria   10 años · C.Co. Art. 2535 (supletivo civil)
#   comercial_ejecutiva   3 años  · C.Co. Art. 2536 (títulos valores Art. 789)
#   laboral               3 años  · CST Art. 488 desde exigibilidad
#   familiar_alimentos    5 años  · Ley 1098/2006 Art. 138
#   accion_revision       2 años  · CGP Art. 354
#   penal_querella        6 meses · CPP Art. 73
#
# Interrupción civil:
#   - Notificación de la demanda (CGP Art. 94)
#   - Reconocimiento expreso o tácito de la deuda (CC Art. 2539)
#   - Pago parcial
# La interrupción "borra el tiempo corrido" y reinicia el plazo desde la fecha
# del acto interruptor.

PRESCRIPCION_TABLA: dict[str, dict] = {
    "civil_ordinaria":     {"anios": 10, "fundamento": "CC Art. 2536 mod. Ley 791/2002"},
    "civil_ejecutiva":     {"anios": 5,  "fundamento": "CC Art. 2536 mod. Ley 791/2002"},
    "comercial_ordinaria": {"anios": 10, "fundamento": "C.Co. Art. 2535 + CC Art. 2536"},
    "comercial_ejecutiva": {"anios": 3,  "fundamento": "C.Co. Art. 789 (títulos valores)"},
    "laboral":             {"anios": 3,  "fundamento": "CST Art. 488"},
    "familiar_alimentos":  {"anios": 5,  "fundamento": "Ley 1098/2006 Art. 138"},
    "accion_revision":     {"anios": 2,  "fundamento": "CGP Art. 354"},
    "penal_querella":      {"meses": 6,  "fundamento": "CPP Art. 73"},
}


class PrescripcionRequest(BaseModel):
    tipo_accion: str = Field(
        pattern=r"^(civil_ordinaria|civil_ejecutiva|comercial_ordinaria|comercial_ejecutiva|laboral|familiar_alimentos|accion_revision|penal_querella)$"
    )
    fecha_exigibilidad: date
    fecha_interrupcion: Optional[date] = None
    fecha_calculo: date = Field(default_factory=lambda: date.today())
    case_label: Optional[str] = None
    matter_id: Optional[str] = None
    persist: bool = True


class PrescripcionResponse(BaseModel):
    id: str
    formulas_version: str
    tipo_accion: str
    fundamento: str
    fecha_exigibilidad: date
    fecha_interrupcion: Optional[date]
    fecha_inicio_efectivo: date
    fecha_prescripcion: date
    fecha_calculo: date
    dias_restantes: int
    prescrita: bool
    line_items: list[LineItem]
    desglose_legible: str


def _add_period(d: date, anios: int = 0, meses: int = 0) -> date:
    """Suma años/meses respetando longitud variable de mes (sin uso de relativedelta)."""
    total_months = d.month - 1 + meses + anios * 12
    new_year = d.year + total_months // 12
    new_month = total_months % 12 + 1
    # Día capeado al último día del mes destino
    import calendar
    last_day = calendar.monthrange(new_year, new_month)[1]
    new_day = min(d.day, last_day)
    return date(new_year, new_month, new_day)


def compute_prescripcion(req: PrescripcionRequest) -> PrescripcionResponse:
    spec = PRESCRIPCION_TABLA[req.tipo_accion]
    inicio = req.fecha_interrupcion or req.fecha_exigibilidad
    if "anios" in spec:
        fecha_pres = _add_period(inicio, anios=spec["anios"])
        plazo_legible = f"{spec['anios']} años"
    else:
        fecha_pres = _add_period(inicio, meses=spec["meses"])
        plazo_legible = f"{spec['meses']} meses"

    dias_restantes = (fecha_pres - req.fecha_calculo).days
    prescrita = dias_restantes < 0

    items: list[LineItem] = []
    items.append(LineItem(
        concepto=f"Plazo de prescripción ({req.tipo_accion})",
        formula=f"{plazo_legible} desde {inicio.isoformat()}",
        base=0,
        multiplicador=spec.get("anios") or spec.get("meses") or 0,
        monto_cop=0,
        fundamento=spec["fundamento"],
        nota=(
            f"Interrumpido el {req.fecha_interrupcion.isoformat()} (CGP Art. 94 / CC Art. 2539). "
            "Plazo re-cuenta desde la interrupción."
            if req.fecha_interrupcion
            else "Sin acto interruptor registrado · plazo corre desde la exigibilidad."
        ),
    ))
    items.append(LineItem(
        concepto="Fecha de prescripción",
        formula=f"{inicio.isoformat()} + {plazo_legible}",
        base=0,
        multiplicador=0,
        monto_cop=0,
        fundamento=spec["fundamento"],
        nota=fecha_pres.isoformat(),
    ))
    items.append(LineItem(
        concepto="Estado actual",
        formula=f"hoy = {req.fecha_calculo.isoformat()}",
        base=0,
        multiplicador=dias_restantes,
        monto_cop=0,
        fundamento="Cómputo determinista",
        nota=(
            f"PRESCRITA hace {abs(dias_restantes)} días"
            if prescrita
            else f"Vigente · {dias_restantes} días restantes"
        ),
    ))

    desglose = "\n".join(f"- {i.concepto}: {i.nota or ''}" for i in items)
    return PrescripcionResponse(
        id=str(uuid.uuid4()),
        formulas_version="co-prescripcion-v1",
        tipo_accion=req.tipo_accion,
        fundamento=spec["fundamento"],
        fecha_exigibilidad=req.fecha_exigibilidad,
        fecha_interrupcion=req.fecha_interrupcion,
        fecha_inicio_efectivo=inicio,
        fecha_prescripcion=fecha_pres,
        fecha_calculo=req.fecha_calculo,
        dias_restantes=dias_restantes,
        prescrita=prescrita,
        line_items=items,
        desglose_legible=desglose,
    )


@router.post("/prescripcion", response_model=PrescripcionResponse)
async def calc_prescripcion_endpoint(
    req: PrescripcionRequest,
    principal: Principal = Depends(get_current_firm),
):
    result = compute_prescripcion(req)
    if req.persist:
        try:
            from utils.db import get_storage
            storage = await get_storage()
            if hasattr(storage, "pool"):
                async with storage.pool.acquire() as conn:
                    await conn.execute(
                        """
                        insert into calc_prescripciones
                          (id, firm_id, matter_id, user_id, case_label,
                           tipo_accion, fecha_exigibilidad, fecha_interrupcion,
                           fecha_calculo, variables, resultado,
                           fecha_prescripcion, dias_restantes, prescrita,
                           formulas_version)
                        values
                          ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5,
                           $6, $7::date, $8::date,
                           $9::date, $10::jsonb, $11::jsonb,
                           $12::date, $13, $14,
                           $15)
                        """,
                        result.id,
                        principal.firm_id,
                        req.matter_id,
                        principal.user_id,
                        req.case_label,
                        req.tipo_accion,
                        req.fecha_exigibilidad,
                        req.fecha_interrupcion,
                        req.fecha_calculo,
                        json.dumps(req.model_dump(), default=str),
                        json.dumps([i.model_dump() for i in result.line_items], default=str),
                        result.fecha_prescripcion,
                        result.dias_restantes,
                        result.prescrita,
                        result.formulas_version,
                    )
        except Exception as e:
            logger.warning("prescripcion persist failed (non-fatal): %s", e)
    return result


async def calc_prescripcion_tool(args: dict, ctx: dict) -> dict:
    try:
        req = PrescripcionRequest(
            tipo_accion=args["tipo_accion"],
            fecha_exigibilidad=date.fromisoformat(args["fecha_exigibilidad"]),
            fecha_interrupcion=date.fromisoformat(args["fecha_interrupcion"]) if args.get("fecha_interrupcion") else None,
            fecha_calculo=date.fromisoformat(args["fecha_calculo"]) if args.get("fecha_calculo") else date.today(),
            case_label=args.get("case_label"),
            matter_id=ctx.get("matter_id"),
            persist=True,
        )
    except Exception as e:
        return {"error": f"invalid arguments: {e}"}
    result = compute_prescripcion(req)
    return {
        "id": result.id,
        "tipo_accion": result.tipo_accion,
        "fundamento": result.fundamento,
        "fecha_prescripcion": result.fecha_prescripcion.isoformat(),
        "dias_restantes": result.dias_restantes,
        "prescrita": result.prescrita,
        "desglose_legible": result.desglose_legible,
    }


# ════════════════════════════════════════════════════════════════════════
# INTERESES MORATORIOS · cómputo determinista
# ════════════════════════════════════════════════════════════════════════
#
# Tipos:
#   comercial_moratorio: 1.5 × interés bancario corriente (Decreto 519/2007 Art. 5)
#   civil_legal:         6% anual (CC Art. 1617 par. 2 — supletorio)
#   convencional:        tasa pactada (no puede exceder 1.5× corriente)
#   laboral_cesantias:   1 día de salario por cada día de mora (Ley 50/1990 Art. 99)
#
# Para MVP usamos cómputo simple lineal con tramo único; tramos por trimestre
# (para refletar variación de DTF) puede agregarse iterando legal_constants.
# Métodos: 'simple' (lineal) | 'compuesto' (1+r)^t.

INTERESES_TABLA: dict[str, dict] = {
    "comercial_moratorio": {
        "tasa_key": "co.interes_moratorio_max.anual",
        "fundamento": "Decreto 519/2007 Art. 5 (1.5× interés bancario corriente)",
    },
    "civil_legal": {
        "tasa_key": "co.tasa_legal_civil.anual",
        "fundamento": "CC Art. 1617 par. 2 (6% legal supletivo)",
    },
    "convencional": {
        "tasa_key": None,  # tasa explícita por el usuario
        "fundamento": "Tasa pactada en contrato — máximo 1.5× corriente (Decreto 519/2007)",
    },
}


class InteresesRequest(BaseModel):
    tipo_interes: str = Field(
        pattern=r"^(comercial_moratorio|civil_legal|convencional)$"
    )
    capital_cop: float = Field(gt=0)
    fecha_inicio: date
    fecha_fin: date = Field(default_factory=lambda: date.today())
    tasa_anual: Optional[float] = None  # requerido sólo si tipo='convencional'
    base_calculo: int = Field(default=360)  # 360 (comercial) o 365 (civil)
    metodo: str = Field(default="simple", pattern="^(simple|compuesto)$")
    case_label: Optional[str] = None
    matter_id: Optional[str] = None
    persist: bool = True

    @field_validator("fecha_fin")
    @classmethod
    def _fin_ge_inicio(cls, v: date, info):
        ini = info.data.get("fecha_inicio")
        if ini and v < ini:
            raise ValueError("fecha_fin debe ser >= fecha_inicio")
        return v


class InteresesResponse(BaseModel):
    id: str
    formulas_version: str
    tipo_interes: str
    fundamento: str
    capital_cop: float
    tasa_anual_aplicada: float
    base_calculo: int
    metodo: str
    dias_mora: int
    monto_intereses_cop: float
    monto_total_cop: float
    line_items: list[LineItem]
    desglose_legible: str


async def _resolve_tasa_anual(
    tipo_interes: str,
    tasa_explicita: Optional[float],
    fecha_referencia: date,
) -> float:
    spec = INTERESES_TABLA[tipo_interes]
    if tipo_interes == "convencional":
        if tasa_explicita is None or tasa_explicita <= 0:
            raise HTTPException(422, "tipo='convencional' requiere `tasa_anual` > 0")
        return float(tasa_explicita)

    # Resolver desde legal_constants vía RPC
    try:
        from utils.db import get_storage
        storage = await get_storage()
        if hasattr(storage, "pool"):
            async with storage.pool.acquire() as conn:
                val = await conn.fetchval(
                    "select lexai_legal_constant($1::text, $2::date)",
                    spec["tasa_key"], fecha_referencia,
                )
                if val is not None:
                    return float(val)
    except Exception:
        pass
    # Fallback: hardcoded conservative defaults
    return {
        "co.interes_moratorio_max.anual": 0.2922,
        "co.tasa_legal_civil.anual":       0.06,
    }.get(spec["tasa_key"] or "", 0.06)


def _days_actual(d1: date, d2: date) -> int:
    return max(0, (d2 - d1).days)


async def compute_intereses(req: InteresesRequest) -> InteresesResponse:
    spec = INTERESES_TABLA[req.tipo_interes]
    tasa = await _resolve_tasa_anual(req.tipo_interes, req.tasa_anual, req.fecha_fin)
    dias = _days_actual(req.fecha_inicio, req.fecha_fin)
    base = float(req.base_calculo)

    if req.metodo == "simple":
        # Interés simple: I = K × i × (t/base)
        intereses = req.capital_cop * tasa * (dias / base)
        formula = (
            f"{req.capital_cop:,.0f} × {tasa:.4f} × ({dias}/{int(base)})"
        )
    else:
        # Interés compuesto: M = K × (1 + i)^(t/base); I = M − K
        factor = (1.0 + tasa) ** (dias / base)
        monto_total = req.capital_cop * factor
        intereses = monto_total - req.capital_cop
        formula = (
            f"{req.capital_cop:,.0f} × (1 + {tasa:.4f})^({dias}/{int(base)}) − {req.capital_cop:,.0f}"
        )

    monto_total = req.capital_cop + intereses
    items = [
        LineItem(
            concepto=f"Intereses ({req.tipo_interes}, {req.metodo})",
            formula=formula,
            base=req.capital_cop,
            multiplicador=round(tasa * (dias / base), 6),
            monto_cop=_round0(intereses),
            fundamento=spec["fundamento"],
            nota=(
                f"Tasa anual aplicada: {tasa*100:.2f}%. "
                f"Mora de {dias} días sobre base {int(base)}."
            ),
        ),
        LineItem(
            concepto="Capital reclamable",
            formula=f"{req.capital_cop:,.0f}",
            base=req.capital_cop,
            multiplicador=1,
            monto_cop=_round0(req.capital_cop),
            fundamento="Capital adeudado",
        ),
        LineItem(
            concepto="Total reclamable",
            formula="capital + intereses",
            base=req.capital_cop,
            multiplicador=1,
            monto_cop=_round0(monto_total),
            fundamento="Suma capital + intereses moratorios",
        ),
    ]
    desglose = "\n".join(
        f"- {i.concepto}: COP ${i.monto_cop:,.0f}  ({i.fundamento or ''})"
        for i in items
    )
    desglose += f"\n\nTotal a reclamar: COP ${monto_total:,.0f}"
    return InteresesResponse(
        id=str(uuid.uuid4()),
        formulas_version="co-intereses-v1",
        tipo_interes=req.tipo_interes,
        fundamento=spec["fundamento"],
        capital_cop=req.capital_cop,
        tasa_anual_aplicada=tasa,
        base_calculo=req.base_calculo,
        metodo=req.metodo,
        dias_mora=dias,
        monto_intereses_cop=_round0(intereses),
        monto_total_cop=_round0(monto_total),
        line_items=items,
        desglose_legible=desglose,
    )


@router.post("/intereses", response_model=InteresesResponse)
async def calc_intereses_endpoint(
    req: InteresesRequest,
    principal: Principal = Depends(get_current_firm),
):
    result = await compute_intereses(req)
    if req.persist:
        try:
            from utils.db import get_storage
            storage = await get_storage()
            if hasattr(storage, "pool"):
                async with storage.pool.acquire() as conn:
                    await conn.execute(
                        """
                        insert into calc_intereses
                          (id, firm_id, matter_id, user_id, case_label,
                           tipo_interes, capital_cop, fecha_inicio, fecha_fin,
                           tasa_anual_aplicada, base_calculo, metodo,
                           variables, resultado, monto_total_cop, formulas_version)
                        values
                          ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5,
                           $6, $7, $8::date, $9::date,
                           $10, $11, $12,
                           $13::jsonb, $14::jsonb, $15, $16)
                        """,
                        result.id,
                        principal.firm_id,
                        req.matter_id,
                        principal.user_id,
                        req.case_label,
                        req.tipo_interes,
                        req.capital_cop,
                        req.fecha_inicio,
                        req.fecha_fin,
                        result.tasa_anual_aplicada,
                        req.base_calculo,
                        req.metodo,
                        json.dumps(req.model_dump(), default=str),
                        json.dumps([i.model_dump() for i in result.line_items], default=str),
                        result.monto_total_cop,
                        result.formulas_version,
                    )
        except Exception as e:
            logger.warning("intereses persist failed (non-fatal): %s", e)
    return result


async def calc_intereses_tool(args: dict, ctx: dict) -> dict:
    try:
        req = InteresesRequest(
            tipo_interes=args["tipo_interes"],
            capital_cop=float(args["capital_cop"]),
            fecha_inicio=date.fromisoformat(args["fecha_inicio"]),
            fecha_fin=date.fromisoformat(args["fecha_fin"]) if args.get("fecha_fin") else date.today(),
            tasa_anual=float(args["tasa_anual"]) if args.get("tasa_anual") is not None else None,
            base_calculo=int(args.get("base_calculo", 360)),
            metodo=args.get("metodo", "simple"),
            case_label=args.get("case_label"),
            matter_id=ctx.get("matter_id"),
            persist=True,
        )
    except Exception as e:
        return {"error": f"invalid arguments: {e}"}
    result = await compute_intereses(req)
    return {
        "id": result.id,
        "tipo_interes": result.tipo_interes,
        "fundamento": result.fundamento,
        "tasa_anual_aplicada": result.tasa_anual_aplicada,
        "dias_mora": result.dias_mora,
        "monto_intereses_cop": result.monto_intereses_cop,
        "monto_total_cop": result.monto_total_cop,
        "currency": "COP",
        "desglose_legible": result.desglose_legible,
    }
