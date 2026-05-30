"""Sprint M20.13 · Tests del estado→tier mapping en verify_citation."""
from __future__ import annotations

import pytest

from lex.tools.verify_citation import _estado_to_tier


class TestEstadoToTier:
    def test_no_encontrada_to_not_found(self):
        assert _estado_to_tier("no_encontrada", False) == "NOT_FOUND"

    def test_superada_to_derogada(self):
        assert _estado_to_tier("superada", False) == "DEROGADA"

    def test_derogada_flag_to_derogada(self):
        assert _estado_to_tier("verificada", True) == "DEROGADA"

    def test_modulada_estado_to_modulada(self):
        """M20.13: 5° tier."""
        assert _estado_to_tier("modulada", False) == "MODULADA"

    def test_modulada_flag_to_modulada(self):
        assert _estado_to_tier("verificada", False, modulada=True) == "MODULADA"

    def test_sospechosa_to_verify_flag(self):
        assert _estado_to_tier("sospechosa", False) == "VERIFY_FLAG"

    def test_verificada_to_grounded(self):
        assert _estado_to_tier("verificada", False) == "GROUNDED"

    def test_unknown_to_verify_flag(self):
        assert _estado_to_tier("estado_raro", False) == "VERIFY_FLAG"

    def test_derogada_overrides_modulada(self):
        # Si está derogada Y modulada, DEROGADA gana (severity más alta)
        assert _estado_to_tier("verificada", derogada=True, modulada=True) == "DEROGADA"

    def test_not_found_overrides_all(self):
        # not_encontrada gana sobre todo
        assert _estado_to_tier("no_encontrada", derogada=True, modulada=True) == "NOT_FOUND"
