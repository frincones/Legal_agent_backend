"""Calculadoras puras por materia. Cero alucinación numérica.

Cada módulo expone funciones puras con:
- inputs tipados
- output dict con valor + fórmula + referencia normativa
- tests pytest con casos verificados
"""
from lex.calc import laboral, civil, penal, alimentos, tributario, intereses_base

__all__ = ["laboral", "civil", "penal", "alimentos", "tributario", "intereses_base"]
