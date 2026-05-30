"""Sprint M20.10 · Tests del 4-tier markers en python_docx_builder."""
from __future__ import annotations

import pytest

from lex.renderer.python_docx_builder import _tier_from_data, build_docx_from_blocks


class TestTierExtraction:
    def test_grounded_explicit(self):
        assert _tier_from_data({"tier": "GROUNDED"}) == "GROUNDED"

    def test_derogada_explicit(self):
        assert _tier_from_data({"tier": "DEROGADA"}) == "DEROGADA"

    def test_verify_flag_explicit(self):
        assert _tier_from_data({"tier": "VERIFY_FLAG"}) == "VERIFY_FLAG"

    def test_not_found_explicit(self):
        assert _tier_from_data({"tier": "NOT_FOUND"}) == "NOT_FOUND"

    def test_modulada_explicit(self):
        """M20.13: 5° tier."""
        assert _tier_from_data({"tier": "MODULADA"}) == "MODULADA"

    def test_legacy_verified_true_maps_grounded(self):
        assert _tier_from_data({"verified": True}) == "GROUNDED"

    def test_legacy_derogada_true_maps_derogada(self):
        assert _tier_from_data({"derogada": True}) == "DEROGADA"

    def test_legacy_no_flags_maps_verify_flag(self):
        assert _tier_from_data({}) == "VERIFY_FLAG"

    def test_invalid_tier_falls_back_to_legacy(self):
        assert _tier_from_data({"tier": "INVALID", "verified": True}) == "GROUNDED"


class TestDocxRendererWithTiers:
    def test_builds_docx_with_grounded_citation(self):
        blocks = [
            {"block_type": "title", "block_data": {"text": "TEST DOC"}},
            {"block_type": "norma_citada",
             "block_data": {
                 "norma": "Art. 2142 CC",
                 "tier": "GROUNDED",
                 "fuente_url_oficial": "https://suin-juriscol.gov.co/...",
                 "contenido": [{"text": "El mandato es..."}],
             }},
        ]
        docx_bytes = build_docx_from_blocks(blocks, title="Test")
        assert isinstance(docx_bytes, bytes)
        assert len(docx_bytes) > 1000

    def test_builds_docx_with_derogada_citation(self):
        blocks = [
            {"block_type": "norma_citada",
             "block_data": {
                 "norma": "Decreto 100 de 1980",
                 "tier": "DEROGADA",
                 "derogada_por": "Ley 599 de 2000",
             }},
        ]
        docx_bytes = build_docx_from_blocks(blocks, title="Test")
        assert isinstance(docx_bytes, bytes)

    def test_builds_docx_with_not_found_citation(self):
        blocks = [
            {"block_type": "norma_citada",
             "block_data": {
                 "norma": "Art. 836 CGP",
                 "tier": "NOT_FOUND",
                 "suggested_correction": "Quizás Art. 836 CC?",
             }},
        ]
        docx_bytes = build_docx_from_blocks(blocks, title="Test")
        assert isinstance(docx_bytes, bytes)

    def test_builds_docx_with_verify_flag(self):
        blocks = [
            {"block_type": "norma_citada",
             "block_data": {"norma": "Algo no verificable", "tier": "VERIFY_FLAG"}},
        ]
        docx_bytes = build_docx_from_blocks(blocks, title="Test")
        assert isinstance(docx_bytes, bytes)

    def test_builds_docx_with_modulada(self):
        """M20.13: 5° tier MODULADA."""
        blocks = [
            {"block_type": "norma_citada",
             "block_data": {
                 "norma": "Ley 1437 de 2011 art. 4",
                 "tier": "MODULADA",
                 "suggested_correction": "C-1011/2008",
             }},
        ]
        docx_bytes = build_docx_from_blocks(blocks, title="Test")
        assert isinstance(docx_bytes, bytes)

    def test_jurisprudencia_with_tier(self):
        blocks = [
            {"block_type": "jurisprudencia",
             "block_data": {
                 "id": "T-067/2025",
                 "corte": "Corte Constitucional",
                 "mp": "Juez X",
                 "tier": "GROUNDED",
                 "fuente_url_oficial": "https://corteconstitucional.gov.co/...",
                 "ratio": [{"text": "El precedente establece..."}],
             }},
        ]
        docx_bytes = build_docx_from_blocks(blocks, title="Test")
        assert isinstance(docx_bytes, bytes)

    def test_backwards_compat_legacy_verified(self):
        """Bloques antiguos con verified=true siguen funcionando."""
        blocks = [
            {"block_type": "norma_citada",
             "block_data": {
                 "norma": "Art. 2142 CC",
                 "verified": True,
                 "fuente_url": "https://suin.../...",
             }},
        ]
        docx_bytes = build_docx_from_blocks(blocks, title="Test")
        assert isinstance(docx_bytes, bytes)
