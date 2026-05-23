"""Stage 4: Calculadora — invoca cálculos Python puros para el template.

Resuelve la calculadora desde template.calculadora (string 'module:function')
y la invoca con extracted_data como kwargs.
"""
from __future__ import annotations

import importlib
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _resolve_callable(path: str):
    """Resuelve 'lex.calc.laboral:full_liquidacion' a la función Python."""
    if ":" not in path:
        return None
    mod_path, func_name = path.split(":", 1)
    try:
        mod = importlib.import_module(mod_path)
        return getattr(mod, func_name, None)
    except Exception as e:
        logger.warning("calculadora resolve failed for %s: %s", path, e)
        return None


def run_calculadora(template_calc_path: str | None, extracted_data: dict[str, Any]) -> dict[str, Any] | None:
    """Ejecuta la calculadora del template si está configurada.

    Returns:
        dict con conceptos + total, o None si no aplica.
    """
    if not template_calc_path:
        return None

    func = _resolve_callable(template_calc_path)
    if func is None:
        return None

    # Mapping de field names del extracted_data a kwargs de la calculadora
    # Best-effort: las calculadoras esperan keywords como salario_mensual, fecha_ingreso, etc.
    kwargs: dict[str, Any] = {}
    field_map = {
        "salario_mensual": ["salario_mensual", "salario"],
        "fecha_ingreso": ["fecha_ingreso"],
        "fecha_termino": ["fecha_despido", "fecha_termino", "fecha_terminacion"],
        "dias_mora": ["dias_mora"],
        "capital": ["monto_capital", "monto_reclamado", "capital"],
        "fecha_desde": ["fecha_titulo", "fecha_hechos", "fecha_desde"],
        "fecha_hasta": ["fecha_liquidacion", "fecha_hasta"],
        "ingresos_alimentante": ["alimentante_ingresos", "ingresos_alimentante"],
        "numero_hijos": ["numero_hijos", "hijos"],
        "pena_minima_meses": ["pena_minima_meses"],
        "pena_maxima_meses": ["pena_maxima_meses"],
    }

    try:
        import inspect
        sig = inspect.signature(func)
        for param_name in sig.parameters:
            sources = field_map.get(param_name, [param_name])
            for s in sources:
                if s in extracted_data and extracted_data[s] not in (None, "", []):
                    kwargs[param_name] = extracted_data[s]
                    break

        # Filtrar kwargs no aceptados
        accepted = {k: v for k, v in kwargs.items() if k in sig.parameters}
        if not accepted:
            logger.info("calculadora skipped: no matching kwargs in extracted_data")
            return None

        result = func(**accepted)
        return result
    except Exception as e:
        logger.warning("calculadora invocation failed (%s): %s", template_calc_path, e)
        return None
