"""Validación determinista de fórmulas de liquidación laboral CST.

Casos golden: 8 escenarios con números verificados a mano. Cualquier
cambio en api/calc.py debe pasar este test.

Run:
    cd backend
    python -m tests.test_liquidacion_formulas
"""

from __future__ import annotations

import os
import sys
from datetime import date

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.calc import LiquidacionRequest, compute_liquidacion, SMLMV_2026  # noqa: E402


def _line(items, concepto_substr: str) -> dict | None:
    for i in items:
        if concepto_substr.lower() in i.concepto.lower():
            return i.model_dump()
    return None


def _close(a: float, b: float, tol: int = 5) -> bool:
    return abs(float(a) - float(b)) <= tol


def case_pedro_480d_no_integral():
    """Pedro · 480 días (~16 meses) · 20M COP · indefinido · injustificado.

    Esperado:
      Cesantías          = 20M × 480 / 360 = 26,666,667
      Intereses          = 26,666,667 × 0.12 × 480/360 = 4,266,667
      Prima              = 20M × 480 / 360 = 26,666,667
      Vacaciones         = (15 × 480/360) × (20M/30) = 20 × 666,667 = 13,333,340
      Indemnización ≥10 SMMLV con fracción:
          = (20 + 15 × (120/360)) × (20M/30)
          = 25 × 666,667 = 16,666,675
      Total ≈ 87,600,019
    """
    req = LiquidacionRequest(
        fecha_ingreso=date(2024, 10, 1),
        fecha_terminacion=date(2026, 2, 1),  # 480 días bajo regla 360
        salario_mensual_cop=20_000_000,
        causa="injustificado",
        tipo_contrato="indefinido",
        salario_integral=False,
        persist=False,
    )
    res = compute_liquidacion(req)
    days = res.inputs["dias_servicio_360"]
    assert days == 480, f"days_total: esperaba 480, obtuvo {days}"
    ces = _line(res.line_items, "Cesantías")
    assert _close(ces["monto_cop"], 26_666_667), ces
    intereses = _line(res.line_items, "Intereses")
    assert _close(intereses["monto_cop"], 4_266_667), intereses
    prima = _line(res.line_items, "Prima de servicios")
    assert _close(prima["monto_cop"], 26_666_667), prima
    vac = _line(res.line_items, "Vacaciones")
    assert _close(vac["monto_cop"], 13_333_333, tol=10), vac
    indemn = _line(res.line_items, "Indemnización")
    assert indemn is not None
    # Fix: 25 días (20 base + 15 × 1/3) × 666,667 = 16,666,675
    assert _close(indemn["monto_cop"], 16_666_675, tol=20), indemn
    print(f"  pedro_480d total: {res.total_cop:,.0f} (esperado ~87,600,000)")
    assert _close(res.total_cop, 87_600_009, tol=50), res.total_cop


def case_maria_7anos_4_5M_no_integral():
    """María · 7 años exactos · 4.5M COP · indefinido · injustificado.

    Esperado bucket: 4.5M / 1.823.500 = 2.47 SMMLV → <10 SMMLV → 30 + 20×6 = 150 días
      Indemnización = 150 × (4.5M/30) = 150 × 150,000 = 22,500,000
    """
    req = LiquidacionRequest(
        fecha_ingreso=date(2018, 1, 15),
        fecha_terminacion=date(2025, 1, 15),
        salario_mensual_cop=4_500_000,
        causa="injustificado",
        tipo_contrato="indefinido",
        salario_integral=False,
        persist=False,
    )
    res = compute_liquidacion(req)
    indemn = _line(res.line_items, "Indemnización")
    # 7 años exactos: 30 días primer año + 20 × 6 = 150 días
    assert _close(indemn["multiplicador"], 150, tol=1), indemn
    assert _close(indemn["monto_cop"], 22_500_000, tol=20), indemn
    print(f"  maria_7anos indemn: {indemn['monto_cop']:,.0f} (esperado 22,500,000)")


def case_integral_apply_correct_bucket():
    """Salario integral 25M (≥10 SMMLV) · 2 años · injustificado.

    Bucket: 25M / 1.823.500 = 13.71 → ≥10 SMMLV → 20 + 15×1 = 35 días
    Base diaria: 25M × 0.70 / 30 = 583,333
    Indemnización = 35 × 583,333 = 20,416,655
    Sin cesantías/intereses/prima/vacaciones (régimen integral).
    """
    req = LiquidacionRequest(
        fecha_ingreso=date(2024, 2, 5),
        fecha_terminacion=date(2026, 2, 5),
        salario_mensual_cop=25_000_000,
        causa="injustificado",
        tipo_contrato="indefinido",
        salario_integral=True,
        persist=False,
    )
    res = compute_liquidacion(req)
    indemn = _line(res.line_items, "Indemnización")
    assert _close(indemn["multiplicador"], 35, tol=1), indemn
    assert _close(indemn["monto_cop"], 20_416_655, tol=20), indemn
    # No prestaciones ordinarias
    assert _line(res.line_items, "Cesantías") is None
    assert _line(res.line_items, "Prima de servicios") is None
    print(f"  integral_25M indemn: {indemn['monto_cop']:,.0f} (esperado 20,416,655)")


def case_integral_under_10_smmlv_rejected():
    """Salario integral marcado pero < 10 SMMLV → debe rechazarse."""
    req = LiquidacionRequest(
        fecha_ingreso=date(2024, 2, 5),
        fecha_terminacion=date(2026, 2, 5),
        salario_mensual_cop=15_000_000,  # < 10 × 1.823.500 = 18.235.000
        causa="injustificado",
        tipo_contrato="indefinido",
        salario_integral=True,
        persist=False,
    )
    try:
        compute_liquidacion(req)
        assert False, "esperaba HTTPException"
    except Exception as e:
        msg = str(e)
        assert "10 SMMLV" in msg or "integral" in msg.lower(), msg
        print("  integral_under_10_smmlv: rechazado correctamente")


def case_renuncia_no_indemn():
    """Renuncia voluntaria → sin indemnización."""
    req = LiquidacionRequest(
        fecha_ingreso=date(2023, 1, 1),
        fecha_terminacion=date(2026, 1, 1),
        salario_mensual_cop=4_000_000,
        causa="renuncia",
        tipo_contrato="indefinido",
        persist=False,
    )
    res = compute_liquidacion(req)
    assert res.aplica_indemnizacion is False
    indemn = _line(res.line_items, "Indemnización")
    assert indemn is None, "renuncia no debe generar indemnización"
    print("  renuncia: sin indemnización OK")


def case_first_year_minimum():
    """Trabajó 100 días (<1 año), salario <10 SMMLV → 30 días mínimo."""
    req = LiquidacionRequest(
        fecha_ingreso=date(2025, 10, 1),
        fecha_terminacion=date(2026, 1, 11),  # 100 días bajo 360
        salario_mensual_cop=3_000_000,
        causa="injustificado",
        tipo_contrato="indefinido",
        persist=False,
    )
    res = compute_liquidacion(req)
    indemn = _line(res.line_items, "Indemnización")
    assert _close(indemn["multiplicador"], 30, tol=1), indemn
    print(f"  first_year_min: {indemn['monto_cop']:,.0f} (mín 30 días <10 SMMLV)")


def case_high_salary_first_year():
    """6 meses, salario 20M (≥10 SMMLV) → mínimo 20 días primer año."""
    req = LiquidacionRequest(
        fecha_ingreso=date(2025, 8, 5),
        fecha_terminacion=date(2026, 2, 5),
        salario_mensual_cop=20_000_000,
        causa="injustificado",
        tipo_contrato="indefinido",
        persist=False,
    )
    res = compute_liquidacion(req)
    indemn = _line(res.line_items, "Indemnización")
    assert _close(indemn["multiplicador"], 20, tol=1), indemn
    print(f"  high_salary_first: {indemn['monto_cop']:,.0f} (mín 20 días ≥10 SMMLV)")


def case_aux_transporte_low_salary():
    """Salario 1.8M (<2 SMMLV) → aplica auxilio de transporte en cesantías."""
    req = LiquidacionRequest(
        fecha_ingreso=date(2025, 1, 1),
        fecha_terminacion=date(2026, 1, 1),
        salario_mensual_cop=1_800_000,
        causa="injustificado",
        tipo_contrato="indefinido",
        persist=False,
    )
    res = compute_liquidacion(req)
    assert res.inputs["auxilio_transporte_aplicado"] is True
    assert res.inputs["base_prestacional"] > 1_800_000  # con aux transp.
    print(f"  aux_transp aplicado: base prestacional {res.inputs['base_prestacional']:,.0f}")


def main() -> int:
    cases = [
        ("Pedro 480d no integral", case_pedro_480d_no_integral),
        ("María 7 años no integral", case_maria_7anos_4_5M_no_integral),
        ("Integral 25M bucket correcto", case_integral_apply_correct_bucket),
        ("Integral <10 SMMLV rechazado", case_integral_under_10_smmlv_rejected),
        ("Renuncia sin indemn", case_renuncia_no_indemn),
        ("Mín 30 días <10 SMMLV", case_first_year_minimum),
        ("Mín 20 días ≥10 SMMLV", case_high_salary_first_year),
        ("Aux transporte <2 SMMLV", case_aux_transporte_low_salary),
    ]
    fails = 0
    for name, fn in cases:
        try:
            print(f"\n[ OK ] {name}")
            fn()
        except AssertionError as e:
            print(f"[FAIL] {name}: {e}")
            fails += 1
        except Exception as e:
            print(f"[FAIL] {name}: {type(e).__name__}: {e}")
            fails += 1
    total = len(cases)
    passed = total - fails
    print(f"\n{passed}/{total} casos OK ({fails} fallos)")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
