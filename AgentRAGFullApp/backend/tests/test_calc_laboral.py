"""Tests pytest para calc/laboral.py.

Validan que los cálculos coincidan con el benchmark de Claude (Anthropic)
sobre caso María Pérez González:
  - Salario $2.500.000
  - Ingreso 15-mar-2019 → Despido 30-abr-2026
  - 7 años, 1 mes, 15 días = 2.565 días (aproximado)
"""
from datetime import date

from lex.calc import laboral


def test_cesantias_caso_maria_perez():
    """Cesantías = (2.500.000 × 2565) / 360 ≈ $17.812.500 sobre todo el periodo."""
    r = laboral.cesantias(2_500_000, "2019-03-15", "2026-04-30")
    # Tolerancia ±5% por diferencias días exactos
    assert 17_000_000 <= r["valor"] <= 18_500_000, f"Got {r['valor']}"
    assert "Cesantías" in r["formula"]
    assert "Art. 249 CST" in r["norma_ref"]


def test_intereses_cesantias_anual():
    r = laboral.intereses_cesantias(2_500_000, dias_periodo=360)
    # 2.5M × 0.12 = 300.000
    assert r["valor"] == 300_000
    assert "12%" in r["formula"]


def test_intereses_cesantias_proporcional_120_dias():
    r = laboral.intereses_cesantias(833_333, dias_periodo=120)
    # 833.333 × 0.12 × 120/360 = 33.333
    assert 32_000 <= r["valor"] <= 34_000


def test_indemnizacion_art_64_salario_bajo():
    """Caso María Pérez: 7 años + 1 mes + 15 días, salario < 10 SMLMV.
    Esperado ~$12.708.333 (matchea benchmark Claude).
    """
    r = laboral.indemnizacion_art_64(2_500_000, "2019-03-15", "2026-04-30")
    # Benchmark Claude: 12.708.333
    # 30 días primer año + 20×6 años adicionales + fracción 45 días × 20/360
    # = 30 + 120 + 2.5 = 152.5 días
    # × 83.333 = ~12.708.333
    assert 12_500_000 <= r["valor"] <= 13_000_000, f"Got {r['valor']}"
    assert "Art. 64 CST" in r["norma_ref"]
    assert r["anios_completos"] == 7


def test_indemnizacion_art_64_salario_alto():
    """Salario > 10 SMLMV (>$14.235.000 en 2025) usa fórmula reducida 20+15."""
    r = laboral.indemnizacion_art_64(20_000_000, "2020-01-01", "2026-01-01")
    # 6 años completos, salario alto
    # 20 días primer año + 15×5 años adicionales = 95 días
    # × (20M/30) = 666.667 × 95 = ~63.333.333
    assert 60_000_000 <= r["valor"] <= 65_000_000
    assert "alto" in r["formula"]


def test_indemnizacion_art_64_solo_fraccion():
    """< 1 año de servicio: solo proporcional."""
    r = laboral.indemnizacion_art_64(2_500_000, "2026-01-01", "2026-07-01")
    # 6 meses = 180 días. 30/360 × 180 = 15 días × 83.333 = ~1.250.000
    assert 1_100_000 <= r["valor"] <= 1_400_000


def test_sancion_moratoria_180_dias():
    r = laboral.sancion_moratoria_art_65(2_500_000, 180)
    # 83.333 × 180 = 15.000.000
    assert 14_900_000 <= r["valor"] <= 15_100_000


def test_sancion_moratoria_tope_24_meses():
    """Máximo 720 días aunque la mora sea mayor."""
    r = laboral.sancion_moratoria_art_65(2_500_000, 1000)
    # Solo cuenta 720 días: 83.333 × 720 = 60.000.000
    assert r["valor"] == 60_000_000
    assert r["dias_efectivos"] == 720
    assert r["dias_totales_mora"] == 1000


def test_vacaciones_un_anio():
    r = laboral.vacaciones(2_500_000, "2025-01-01", "2026-01-01")
    # 360 días × 15/360 × 83.333 = 1.250.000
    assert 1_200_000 <= r["valor"] <= 1_300_000


def test_prima_servicios_caso_maria_perez():
    r = laboral.prima_servicios(2_500_000, "2019-03-15", "2026-04-30")
    # ~7.13 años × 2.5M = ~17.812.500
    assert 17_000_000 <= r["valor"] <= 18_500_000


def test_full_liquidacion_caso_maria_perez():
    """Liquidación completa caso María Pérez sin sanción moratoria.
    Calcula TODOS los conceptos retroactivos del periodo (no solo pendientes).
    Total = cesantías 7.13 años + intereses + vacaciones + prima + indemnización.
    ≈ 17.8M + 2.1M + 7.5M + 17.8M + 12.7M = ~58M-60M
    """
    r = laboral.full_liquidacion(
        salario_mensual=2_500_000,
        fecha_ingreso="2019-03-15",
        fecha_termino="2026-04-30",
        dias_mora=0,
    )
    assert 50_000_000 <= r["total"] <= 65_000_000, f"Got {r['total']}"
    assert "cesantias" in r["conceptos"]
    assert "indemnizacion_art_64" in r["conceptos"]
    assert "sancion_moratoria_art_65" not in r["conceptos"]
    assert r["parametros"]["anio_termino"] == 2026


def test_full_liquidacion_con_sancion_moratoria():
    """Con dias_mora=720 (tope), suma 60M extra a la liquidación completa."""
    r = laboral.full_liquidacion(
        salario_mensual=2_500_000,
        fecha_ingreso="2019-03-15",
        fecha_termino="2026-04-30",
        dias_mora=720,
    )
    # ~60M base + 60M sanción = ~120M
    assert 110_000_000 <= r["total"] <= 130_000_000, f"Got {r['total']}"
    assert "sancion_moratoria_art_65" in r["conceptos"]
    assert r["conceptos"]["sancion_moratoria_art_65"]["valor"] == 60_000_000


def test_dias_servicio_calculo():
    """Verificar que el cálculo de tiempo servido sea ~2570 días."""
    r = laboral.full_liquidacion(
        salario_mensual=2_500_000,
        fecha_ingreso="2019-03-15",
        fecha_termino="2026-04-30",
    )
    assert 2500 <= r["parametros"]["dias_servicio"] <= 2620
