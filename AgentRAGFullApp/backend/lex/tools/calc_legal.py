"""Tool 15 · calc_legal · wrapper unificado sobre lex/calc/*."""
from __future__ import annotations

import logging
from typing import Any, Optional

from .base import ToolContext, ToolDef, ToolError

logger = logging.getLogger(__name__)


VALID_TIPOS = {
    "intereses",
    "intereses_moratorios_comerciales",
    "laboral",
    "liquidacion_laboral",
    "civil",
    "indemnizacion_civil",
    "penal",
    "alimentos",
    "tributario",
    "prescripcion",
    "plazos",
    "plazos_procesales",
    "pension",
}


class CalcLegalTool(ToolDef):
    name = "calc_legal"
    description = (
        "Ejecuta cálculos legales colombianos especializados: intereses moratorios "
        "(DTF+pct, CCo art. 884), liquidación laboral (CST + prestaciones), "
        "indemnización civil, multas penales, pensión alimenticia, prescripción "
        "(CC/CCo), plazos procesales (CGP), cálculos pensionales (Ley 100). "
        "Usa las constantes legales 2026 (SMLMV, vacancia judicial, DTF). "
        "Llamar cuando el documento incluya pretensiones de pago, liquidaciones "
        "o cálculo de plazos."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "tipo": {
                "type": "string",
                "enum": sorted(VALID_TIPOS),
            },
            "params": {"type": "object", "description": "Parámetros específicos del cálculo (capital, fechas, etc.)"},
            "arbitrary_code": {
                "type": "boolean",
                "default": False,
                "description": "Si true, ejecuta código Python en sandbox (S8). Por defecto usa módulos calc/* directos.",
            },
        },
        "required": ["tipo", "params"],
    }
    timeout_seconds = 20.0

    def __init__(self, **_: Any):
        pass

    async def run(
        self,
        ctx: ToolContext,
        tipo: str,
        params: dict,
        arbitrary_code: bool = False,
    ) -> dict:
        if arbitrary_code:
            raise ToolError("arbitrary_code requires sandbox runtime (sprint S8, no disponible aún)")

        tipo_lower = tipo.lower().strip()
        try:
            if tipo_lower in ("intereses", "intereses_moratorios_comerciales"):
                return await self._calc_intereses(params)
            if tipo_lower in ("laboral", "liquidacion_laboral"):
                return await self._calc_laboral(params)
            if tipo_lower in ("civil", "indemnizacion_civil"):
                return await self._calc_civil(params)
            if tipo_lower == "penal":
                return await self._calc_penal(params)
            if tipo_lower == "alimentos":
                return await self._calc_alimentos(params)
            if tipo_lower == "tributario":
                return await self._calc_tributario(params)
            if tipo_lower == "prescripcion":
                return await self._calc_prescripcion(params)
            if tipo_lower in ("plazos", "plazos_procesales"):
                return await self._calc_plazos(params)
            if tipo_lower == "pension":
                return await self._calc_pension(params)
        except ToolError:
            raise
        except Exception as e:
            logger.exception("calc_legal %s failed", tipo)
            raise ToolError(f"calc {tipo} falló: {e}") from e

        raise ToolError(f"tipo {tipo!r} no soportado")

    # ---- delegations a módulos lex/calc/* ----

    async def _calc_intereses(self, p: dict) -> dict:
        from lex.calc.intereses_base import calcular_intereses_moratorios
        result = calcular_intereses_moratorios(**p)
        return {"tipo": "intereses", "result": result}

    async def _calc_laboral(self, p: dict) -> dict:
        from lex.calc.laboral import liquidar_contrato
        result = liquidar_contrato(**p)
        return {"tipo": "laboral", "result": result}

    async def _calc_civil(self, p: dict) -> dict:
        from lex.calc.civil import calcular_indemnizacion
        result = calcular_indemnizacion(**p)
        return {"tipo": "civil", "result": result}

    async def _calc_penal(self, p: dict) -> dict:
        from lex.calc.penal import calcular_multa
        result = calcular_multa(**p)
        return {"tipo": "penal", "result": result}

    async def _calc_alimentos(self, p: dict) -> dict:
        from lex.calc.alimentos import calcular_alimentos
        result = calcular_alimentos(**p)
        return {"tipo": "alimentos", "result": result}

    async def _calc_tributario(self, p: dict) -> dict:
        from lex.calc.tributario import calcular_impuesto
        result = calcular_impuesto(**p)
        return {"tipo": "tributario", "result": result}

    async def _calc_prescripcion(self, p: dict) -> dict:
        # módulo no implementado uniformemente; usar fórmula básica
        from datetime import date, timedelta
        fecha_inicio = p.get("fecha_inicio")
        anos = int(p.get("anos", 3))
        if isinstance(fecha_inicio, str):
            from datetime import datetime
            fecha_inicio = datetime.strptime(fecha_inicio, "%Y-%m-%d").date()
        fecha_vence = fecha_inicio + timedelta(days=int(anos * 365.25))
        return {
            "tipo": "prescripcion",
            "fecha_inicio": str(fecha_inicio),
            "anos": anos,
            "fecha_vence": str(fecha_vence),
            "vigente_a_hoy": date.today() < fecha_vence,
        }

    async def _calc_plazos(self, p: dict) -> dict:
        # cálculo simple de días hábiles (sin vacancia detallada; producción usar feriados table)
        from datetime import date, timedelta, datetime
        inicio = p.get("fecha_inicio")
        dias = int(p.get("dias_habiles", 0))
        if isinstance(inicio, str):
            inicio = datetime.strptime(inicio, "%Y-%m-%d").date()
        count = 0
        d = inicio
        while count < dias:
            d += timedelta(days=1)
            if d.weekday() < 5:   # L-V
                count += 1
        return {
            "tipo": "plazos",
            "fecha_inicio": str(inicio),
            "dias_habiles": dias,
            "fecha_vence": str(d),
        }

    async def _calc_pension(self, p: dict) -> dict:
        # módulo simple: semanas cotizadas vs requeridas
        semanas = int(p.get("semanas_cotizadas", 0))
        edad = int(p.get("edad", 0))
        genero = (p.get("genero") or "").lower()
        edad_min = 57 if genero == "f" else 62
        semanas_min = 1300
        return {
            "tipo": "pension",
            "semanas_cotizadas": semanas,
            "edad": edad,
            "edad_minima": edad_min,
            "semanas_minimas": semanas_min,
            "cumple_edad": edad >= edad_min,
            "cumple_semanas": semanas >= semanas_min,
            "tiene_derecho": (edad >= edad_min) and (semanas >= semanas_min),
        }


def build_tool(**_: Any) -> ToolDef:
    return CalcLegalTool()
