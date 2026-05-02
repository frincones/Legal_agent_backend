"""Golden tests for prescripcion + intereses calculators (F3)."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.calc import (  # noqa: E402
    PrescripcionRequest,
    InteresesRequest,
    compute_prescripcion,
    compute_intereses,
)


# ─── Helpers ──────────────────────────────────────────────────────────


def _close(a, b, tol=2):
    return abs(float(a) - float(b)) <= tol


# ════════════════════════════════════════════════════════════════════════
# PRESCRIPCION · 12 casos golden
# ════════════════════════════════════════════════════════════════════════


def case_civil_ordinaria_10_anos():
    req = PrescripcionRequest(
        tipo_accion="civil_ordinaria",
        fecha_exigibilidad=date(2020, 5, 15),
        fecha_calculo=date(2026, 5, 2),
        persist=False,
    )
    r = compute_prescripcion(req)
    assert r.fecha_prescripcion == date(2030, 5, 15), r.fecha_prescripcion
    assert r.prescrita is False
    print(f"  civil_ordinaria 10a: prescribe {r.fecha_prescripcion} ({r.dias_restantes}d restantes) OK")


def case_civil_ejecutiva_5_anos():
    req = PrescripcionRequest(
        tipo_accion="civil_ejecutiva",
        fecha_exigibilidad=date(2020, 5, 15),
        fecha_calculo=date(2026, 5, 2),
        persist=False,
    )
    r = compute_prescripcion(req)
    assert r.fecha_prescripcion == date(2025, 5, 15)
    assert r.prescrita is True
    print(f"  civil_ejecutiva 5a: PRESCRITA hace {abs(r.dias_restantes)}d OK")


def case_laboral_3_anos_cst_488():
    req = PrescripcionRequest(
        tipo_accion="laboral",
        fecha_exigibilidad=date(2024, 1, 15),
        fecha_calculo=date(2026, 5, 2),
        persist=False,
    )
    r = compute_prescripcion(req)
    assert r.fecha_prescripcion == date(2027, 1, 15)
    assert r.prescrita is False
    assert "CST Art. 488" in r.fundamento
    print(f"  laboral CST 488: vence {r.fecha_prescripcion} OK")


def case_comercial_ejecutiva_3_anos():
    req = PrescripcionRequest(
        tipo_accion="comercial_ejecutiva",
        fecha_exigibilidad=date(2022, 6, 1),
        fecha_calculo=date(2026, 5, 2),
        persist=False,
    )
    r = compute_prescripcion(req)
    assert r.fecha_prescripcion == date(2025, 6, 1)
    assert r.prescrita is True
    print(f"  comercial_ejecutiva 3a: PRESCRITA OK")


def case_familiar_alimentos_5_anos():
    req = PrescripcionRequest(
        tipo_accion="familiar_alimentos",
        fecha_exigibilidad=date(2023, 12, 1),
        fecha_calculo=date(2026, 5, 2),
        persist=False,
    )
    r = compute_prescripcion(req)
    assert r.fecha_prescripcion == date(2028, 12, 1)
    print(f"  familiar_alimentos 5a: vence {r.fecha_prescripcion} OK")


def case_penal_querella_6_meses():
    req = PrescripcionRequest(
        tipo_accion="penal_querella",
        fecha_exigibilidad=date(2026, 1, 15),
        fecha_calculo=date(2026, 5, 2),
        persist=False,
    )
    r = compute_prescripcion(req)
    assert r.fecha_prescripcion == date(2026, 7, 15)
    print(f"  penal_querella 6m: vence {r.fecha_prescripcion} OK")


def case_interrupcion_civil_cgp_94():
    """Interrupción por notificación de demanda re-inicia el plazo."""
    req = PrescripcionRequest(
        tipo_accion="civil_ordinaria",
        fecha_exigibilidad=date(2018, 1, 1),
        fecha_interrupcion=date(2024, 6, 15),
        fecha_calculo=date(2026, 5, 2),
        persist=False,
    )
    r = compute_prescripcion(req)
    # Plazo se cuenta desde 2024-06-15 + 10 años = 2034-06-15
    assert r.fecha_prescripcion == date(2034, 6, 15)
    assert r.fecha_inicio_efectivo == date(2024, 6, 15)
    print(f"  interrupcion CGP 94: re-inicia desde {r.fecha_inicio_efectivo} -> {r.fecha_prescripcion} OK")


def case_accion_revision_cgp_354():
    req = PrescripcionRequest(
        tipo_accion="accion_revision",
        fecha_exigibilidad=date(2025, 3, 1),
        fecha_calculo=date(2026, 5, 2),
        persist=False,
    )
    r = compute_prescripcion(req)
    assert r.fecha_prescripcion == date(2027, 3, 1)
    print(f"  accion_revision 2a: vence {r.fecha_prescripcion} OK")


def case_dia_exacto_prescripcion():
    """Cuando hoy = fecha_prescripcion, dias_restantes = 0 y NO prescrita."""
    req = PrescripcionRequest(
        tipo_accion="laboral",
        fecha_exigibilidad=date(2023, 5, 2),
        fecha_calculo=date(2026, 5, 2),
        persist=False,
    )
    r = compute_prescripcion(req)
    assert r.dias_restantes == 0
    assert r.prescrita is False
    print(f"  dia_exacto: dias_restantes=0 NOT prescrita OK")


def case_un_dia_prescrita():
    req = PrescripcionRequest(
        tipo_accion="laboral",
        fecha_exigibilidad=date(2023, 5, 1),
        fecha_calculo=date(2026, 5, 2),
        persist=False,
    )
    r = compute_prescripcion(req)
    assert r.dias_restantes == -1
    assert r.prescrita is True
    print(f"  un_dia_prescrita: dias_restantes=-1 OK")


def case_anio_bisiesto():
    """29-feb cae en mes con 28 días → debe ajustar al 28."""
    req = PrescripcionRequest(
        tipo_accion="comercial_ejecutiva",
        fecha_exigibilidad=date(2024, 2, 29),
        fecha_calculo=date(2026, 5, 2),
        persist=False,
    )
    r = compute_prescripcion(req)
    # 2024-02-29 + 3 años = 2027-02-28 (no existe 29-feb en 2027)
    assert r.fecha_prescripcion == date(2027, 2, 28)
    print(f"  bisiesto 29-feb: ajusta a {r.fecha_prescripcion} OK")


def case_fundamento_correcto():
    """Cada tipo debe traer fundamento legal explícito."""
    for tipo, expected_substr in [
        ("civil_ordinaria", "Ley 791"),
        ("laboral", "CST Art. 488"),
        ("comercial_ejecutiva", "C.Co. Art. 789"),
        ("familiar_alimentos", "Ley 1098/2006"),
        ("accion_revision", "CGP Art. 354"),
        ("penal_querella", "CPP Art. 73"),
    ]:
        req = PrescripcionRequest(
            tipo_accion=tipo,
            fecha_exigibilidad=date(2024, 1, 1),
            persist=False,
        )
        r = compute_prescripcion(req)
        assert expected_substr in r.fundamento, f"{tipo}: '{expected_substr}' not in '{r.fundamento}'"
    print("  fundamentos legales: todos los tipos OK")


# ════════════════════════════════════════════════════════════════════════
# INTERESES · 10 casos golden
# ════════════════════════════════════════════════════════════════════════


async def case_comercial_simple_1_ano():
    """1M COP × 29.22% × 360/360 = 292,200 (intereses anuales simples)."""
    req = InteresesRequest(
        tipo_interes="comercial_moratorio",
        capital_cop=1_000_000,
        fecha_inicio=date(2025, 5, 2),
        fecha_fin=date(2026, 5, 2),  # 365 días reales
        base_calculo=360,
        metodo="simple",
        persist=False,
    )
    r = await compute_intereses(req)
    # 365/360 × 0.2922 × 1M ≈ 296,258
    expected = 1_000_000 * 0.2922 * (365 / 360)
    assert _close(r.monto_intereses_cop, round(expected), tol=5), (r.monto_intereses_cop, expected)
    print(f"  comercial_simple_1y: intereses {r.monto_intereses_cop:,.0f} (esperado ~{expected:,.0f}) OK")


async def case_civil_legal_6pct():
    req = InteresesRequest(
        tipo_interes="civil_legal",
        capital_cop=10_000_000,
        fecha_inicio=date(2025, 5, 2),
        fecha_fin=date(2026, 5, 2),
        base_calculo=365,
        metodo="simple",
        persist=False,
    )
    r = await compute_intereses(req)
    # 10M × 6% × 365/365 = 600,000
    assert _close(r.monto_intereses_cop, 600_000, tol=5)
    assert _close(r.tasa_anual_aplicada, 0.06, tol=0.001)
    print(f"  civil_legal 6%: intereses {r.monto_intereses_cop:,.0f} OK")


async def case_convencional_explicit_rate():
    req = InteresesRequest(
        tipo_interes="convencional",
        capital_cop=5_000_000,
        fecha_inicio=date(2025, 5, 2),
        fecha_fin=date(2026, 5, 2),
        tasa_anual=0.20,
        base_calculo=360,
        metodo="simple",
        persist=False,
    )
    r = await compute_intereses(req)
    expected = 5_000_000 * 0.20 * (365 / 360)
    assert _close(r.monto_intereses_cop, round(expected), tol=5)
    assert _close(r.tasa_anual_aplicada, 0.20, tol=0.001)
    print(f"  convencional 20%: intereses {r.monto_intereses_cop:,.0f} OK")


async def case_convencional_sin_tasa_falla():
    """tipo='convencional' SIN tasa_anual debe fallar."""
    req = InteresesRequest(
        tipo_interes="convencional",
        capital_cop=1_000_000,
        fecha_inicio=date(2025, 1, 1),
        persist=False,
    )
    try:
        await compute_intereses(req)
        assert False, "esperaba HTTPException"
    except Exception as e:
        assert "convencional" in str(e).lower() or "tasa_anual" in str(e).lower()
        print("  convencional_sin_tasa: rechazado correctamente OK")


async def case_compuesto_vs_simple():
    """A misma tasa y plazo, compuesto > simple."""
    common = dict(
        tipo_interes="comercial_moratorio",
        capital_cop=10_000_000,
        fecha_inicio=date(2024, 5, 2),
        fecha_fin=date(2026, 5, 2),
        base_calculo=360,
        persist=False,
    )
    rs = await compute_intereses(InteresesRequest(**common, metodo="simple"))
    rc = await compute_intereses(InteresesRequest(**common, metodo="compuesto"))
    assert rc.monto_intereses_cop > rs.monto_intereses_cop
    print(f"  compuesto>simple: simple={rs.monto_intereses_cop:,.0f} compuesto={rc.monto_intereses_cop:,.0f} OK")


async def case_dias_cero():
    """Misma fecha → 0 días → 0 intereses."""
    req = InteresesRequest(
        tipo_interes="civil_legal",
        capital_cop=1_000_000,
        fecha_inicio=date(2026, 5, 2),
        fecha_fin=date(2026, 5, 2),
        persist=False,
    )
    r = await compute_intereses(req)
    assert r.dias_mora == 0
    assert r.monto_intereses_cop == 0
    assert r.monto_total_cop == 1_000_000
    print(f"  dias_cero: intereses=0 total=capital OK")


async def case_capital_grande_no_overflow():
    req = InteresesRequest(
        tipo_interes="comercial_moratorio",
        capital_cop=500_000_000,  # 500 M COP
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2026, 5, 2),
        metodo="compuesto",
        persist=False,
    )
    r = await compute_intereses(req)
    # ~28 meses × 29.22% compuesto sobre 500M
    assert r.monto_intereses_cop > 200_000_000
    assert r.monto_intereses_cop < 1_000_000_000
    print(f"  capital_grande: intereses {r.monto_intereses_cop:,.0f} dentro de rango OK")


async def case_fecha_fin_antes_inicio_falla():
    try:
        InteresesRequest(
            tipo_interes="civil_legal",
            capital_cop=1_000_000,
            fecha_inicio=date(2026, 5, 2),
            fecha_fin=date(2025, 1, 1),
            persist=False,
        )
        assert False, "esperaba ValueError"
    except Exception as e:
        assert "fecha_fin" in str(e) or "fecha_inicio" in str(e)
        print("  fecha_fin<inicio: rechazado OK")


async def case_capital_cero_falla():
    try:
        InteresesRequest(
            tipo_interes="civil_legal",
            capital_cop=0,
            fecha_inicio=date(2025, 1, 1),
            persist=False,
        )
        assert False, "esperaba ValueError"
    except Exception:
        print("  capital_cero: rechazado OK")


async def case_base_365_civil():
    """Civil suele usar base 365; verificar."""
    req = InteresesRequest(
        tipo_interes="civil_legal",
        capital_cop=1_000_000,
        fecha_inicio=date(2025, 5, 2),
        fecha_fin=date(2026, 5, 2),
        base_calculo=365,
        metodo="simple",
        persist=False,
    )
    r = await compute_intereses(req)
    # 1M × 6% × 365/365 = 60,000
    assert _close(r.monto_intereses_cop, 60_000, tol=2)
    assert r.base_calculo == 365
    print(f"  base_365_civil: intereses {r.monto_intereses_cop:,.0f} OK")


# ════════════════════════════════════════════════════════════════════════
# Runner
# ════════════════════════════════════════════════════════════════════════


async def main() -> int:
    sync_cases = [
        ("Civil ordinaria 10 años", case_civil_ordinaria_10_anos),
        ("Civil ejecutiva 5 años", case_civil_ejecutiva_5_anos),
        ("Laboral 3 años CST 488", case_laboral_3_anos_cst_488),
        ("Comercial ejecutiva 3 años", case_comercial_ejecutiva_3_anos),
        ("Familiar alimentos 5 años", case_familiar_alimentos_5_anos),
        ("Penal querella 6 meses", case_penal_querella_6_meses),
        ("Interrupción CGP 94", case_interrupcion_civil_cgp_94),
        ("Acción revisión CGP 354", case_accion_revision_cgp_354),
        ("Día exacto prescripción", case_dia_exacto_prescripcion),
        ("Un día prescrita", case_un_dia_prescrita),
        ("Año bisiesto 29-feb", case_anio_bisiesto),
        ("Fundamentos correctos", case_fundamento_correcto),
    ]
    async_cases = [
        ("Comercial simple 1 año", case_comercial_simple_1_ano),
        ("Civil legal 6%", case_civil_legal_6pct),
        ("Convencional con tasa", case_convencional_explicit_rate),
        ("Convencional sin tasa falla", case_convencional_sin_tasa_falla),
        ("Compuesto > simple", case_compuesto_vs_simple),
        ("Días cero", case_dias_cero),
        ("Capital grande sin overflow", case_capital_grande_no_overflow),
        ("Fecha_fin < inicio falla", case_fecha_fin_antes_inicio_falla),
        ("Capital cero falla", case_capital_cero_falla),
        ("Base 365 civil", case_base_365_civil),
    ]

    fails: list[str] = []
    print("=== PRESCRIPCION ===")
    for name, fn in sync_cases:
        try:
            print(f"\n[ OK ] {name}")
            fn()
        except AssertionError as e:
            print(f"[FAIL] {name}: {e}")
            fails.append(name)
        except Exception as e:
            print(f"[FAIL] {name}: {type(e).__name__}: {e}")
            fails.append(name)

    print("\n=== INTERESES ===")
    for name, fn in async_cases:
        try:
            print(f"\n[ OK ] {name}")
            await fn()
        except AssertionError as e:
            print(f"[FAIL] {name}: {e}")
            fails.append(name)
        except Exception as e:
            print(f"[FAIL] {name}: {type(e).__name__}: {e}")
            fails.append(name)

    total = len(sync_cases) + len(async_cases)
    passed = total - len(fails)
    print(f"\n{passed}/{total} casos OK ({len(fails)} fallos)")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
