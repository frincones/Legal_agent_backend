"""Sprint M20.07 · Tests del Opus complexity score selector."""
from __future__ import annotations

import pytest

from lex.brain.complexity_score import (
    ALWAYS_OPUS_DOC_TYPES,
    NEVER_OPUS_DOC_TYPES,
    compute_complexity_score,
    should_use_opus,
)


class TestComplexityScore:
    def test_short_simple_intent_low_score(self):
        s = compute_complexity_score("poder_especial",
                                       "Poder para Pedro CC 1234.", "")
        assert s < 0.3

    def test_long_intent_with_citations_high_score(self):
        intent = (
            "Demanda civil ordinaria por incumplimiento del contrato de obra "
            "firmado el 15 de marzo de 2024 entre INVERSIONES TRINIDAD SAS y "
            "CONSTRUCTORA ANDINA LTDA. Cuantía $850 millones. Citar Arts. 1546, "
            "1602, 1603, 1613, 1614 del Código Civil y CCo Art. 884 sobre "
            "intereses moratorios. Precedente SC2879-2019 Sala Civil sobre "
            "cláusula penal abusiva. Análisis complejo de varios fuero."
        )
        s = compute_complexity_score("demanda_civil_ordinaria", intent, "")
        assert s >= 0.4   # length 0.2 + citations 0.05*5=0.25 + keyword=0.05 = 0.55 minus matter

    def test_with_matter_metadata_increases_score(self):
        intent = "Contestación demanda."
        no_matter = compute_complexity_score("contestacion_demanda", intent, "")
        with_matter = compute_complexity_score(
            "contestacion_demanda", intent, "",
            matter_metadata={"documents_count": 8},
        )
        assert with_matter > no_matter


class TestShouldUseOpus:
    @pytest.mark.parametrize("doc_type", sorted(ALWAYS_OPUS_DOC_TYPES))
    def test_always_opus_doc_types(self, doc_type):
        use, breakdown = should_use_opus(doc_type, "X", "")
        assert use is True
        assert breakdown["rule"] == "always_opus_doc_type"

    @pytest.mark.parametrize("doc_type", sorted(NEVER_OPUS_DOC_TYPES))
    def test_never_opus_doc_types(self, doc_type):
        use, breakdown = should_use_opus(doc_type, "X" * 5000, "Y" * 5000)
        assert use is False
        assert breakdown["rule"] == "never_opus_doc_type"

    def test_intermediate_simple_uses_sonnet(self):
        use, _ = should_use_opus("contestacion_demanda", "Texto corto.", "")
        assert use is False

    def test_intermediate_complex_uses_opus(self):
        intent = (
            "Recurso amplio de casación contra sentencia compleja con fuero "
            "sindical, cláusula penal abusiva, multidisciplinaria. Citar "
            "Arts. 1546, 1602, 1614 CC y Sentencia SC2879-2019. Análisis "
            "amplio de naturaleza compleja."
        ) * 4
        use, breakdown = should_use_opus(
            "recurso_apelacion", intent, "",
            matter_metadata={"documents_count": 6},
        )
        assert breakdown["complexity_score"] >= 0.65
        assert use is True

    def test_custom_threshold(self):
        # texto largo con algunas citas → score moderado, sensible al threshold
        intent = "Recurso con Art. 320 CGP y Sentencia SC2879-2019 sobre asuntos complejos. " * 5
        use_strict, _ = should_use_opus("recurso_apelacion", intent, "",
                                          threshold=0.9)
        use_lenient, _ = should_use_opus("recurso_apelacion", intent, "",
                                           threshold=0.1)
        assert use_lenient is True
        assert use_strict is False
