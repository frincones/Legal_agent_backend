"""Sprint M20.13 · Tests del derogation_detector heurístico."""
from __future__ import annotations

import pytest

from lex.verify.derogation_detector import (
    KNOWN_DEROGATIONS,
    KNOWN_MODULATIONS,
    detect_explicit_derogation,
    detect_modulation,
)


class TestExplicitDerogation:
    def test_ley_1395_derogada_por_cgp(self):
        r = detect_explicit_derogation("Art. 5 Ley 1395 de 2010")
        assert r is not None
        assert r.tier == "DEROGADA"
        assert "CGP" in r.derogada_por
        assert r.confidence >= 0.9

    def test_decreto_100_1980_codigo_penal_antiguo(self):
        r = detect_explicit_derogation("Decreto 100 de 1980 art. 50")
        assert r is not None
        assert r.tier == "DEROGADA"
        assert "Ley 599" in r.derogada_por

    def test_codigo_procedimiento_civil_legacy(self):
        r = detect_explicit_derogation("Código de Procedimiento Civil art. 100")
        assert r is not None
        assert r.tier == "DEROGADA"

    def test_cpc_alias(self):
        r = detect_explicit_derogation("Art. 100 CPC")
        assert r is not None
        assert r.tier == "DEROGADA"

    def test_norma_vigente_no_match(self):
        r = detect_explicit_derogation("Art. 1546 del Código Civil")
        assert r is None

    def test_random_text_no_match(self):
        r = detect_explicit_derogation("Esto es un texto random sin normas")
        assert r is None


class TestModulation:
    def test_ley_1437_art_4_modulada(self):
        r = detect_modulation("Ley 1437 de 2011 art. 4")
        assert r is not None
        assert r.tier == "MODULADA"
        assert "C-1011/2008" == r.modulada_por

    def test_ley_599_art_122_modulada(self):
        r = detect_modulation("Ley 599 de 2000 art. 122")
        assert r is not None
        assert r.tier == "MODULADA"
        assert "C-355/2006" == r.modulada_por

    def test_norma_no_modulada(self):
        r = detect_modulation("Art. 1546 CC")
        assert r is None


class TestKnownPatternsCoverage:
    def test_known_derogations_not_empty(self):
        assert len(KNOWN_DEROGATIONS) >= 5

    def test_known_modulations_not_empty(self):
        assert len(KNOWN_MODULATIONS) >= 3

    def test_each_derogation_has_3_elements(self):
        for entry in KNOWN_DEROGATIONS:
            assert len(entry) == 3
            assert isinstance(entry[0], str)   # pattern
            assert isinstance(entry[1], str)   # derogada_por
            assert isinstance(entry[2], int)   # año

    def test_each_modulation_has_3_elements(self):
        for entry in KNOWN_MODULATIONS:
            assert len(entry) == 3
