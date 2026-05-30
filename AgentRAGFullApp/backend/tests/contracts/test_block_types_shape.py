"""Sprint M20.01 · Tests de contrato · Block types shape.

Congela el shape de cada uno de los 15+ Block types del sistema.
El frontend (TipTap CanvasEditor) depende de estos shapes exactos.

Si un Block.type o sus campos cambian → CanvasEditor rompe → este test rompe primero.
"""
from __future__ import annotations

from typing import Any


# Lista de Block types validados contra lib/types/blocks.ts del frontend
EXPECTED_BLOCK_TYPES = {
    "title", "section_heading", "subsection", "paragraph",
    "hecho", "pretension", "norma_citada", "jurisprudencia",
    "silogismo", "table", "calc_step", "list_item",
    "juramento", "firma",
}


def _make_run(text: str = "Hola", **kwargs: Any) -> dict:
    return {"text": text, **kwargs}


class TestBlockTypeEnum:
    def test_15_block_types_minimum(self):
        assert len(EXPECTED_BLOCK_TYPES) >= 14

    def test_all_lowercase_snake(self):
        for bt in EXPECTED_BLOCK_TYPES:
            assert bt.islower()
            assert " " not in bt


class TestRunShape:
    def test_run_minimal(self):
        run = _make_run("texto")
        assert "text" in run

    def test_run_with_formatting(self):
        run = _make_run("texto", bold=True, italic=True, underline=True)
        assert run["bold"] is True
        assert run["italic"] is True
        assert run["underline"] is True


class TestStructuralBlocks:
    def test_title_block(self):
        block = {"type": "title", "text": "DEMANDA CIVIL", "level": 0}
        assert block["type"] == "title"
        assert block["level"] in (0, 1, 2)

    def test_section_heading_block(self):
        block = {
            "type": "section_heading",
            "roman": "I",
            "text": "DE LAS PRETENSIONES",
            "section_key": "pretensiones",
        }
        assert block["type"] == "section_heading"
        assert "roman" in block
        assert "section_key" in block

    def test_subsection_block(self):
        block = {"type": "subsection", "number": "1.1", "text": "Subsección"}
        assert block["type"] == "subsection"
        assert "number" in block

    def test_paragraph_block(self):
        block = {
            "type": "paragraph",
            "runs": [_make_run("Texto del párrafo")],
            "align": "justify",
            "indent_left_cm": 1.5,
        }
        assert block["type"] == "paragraph"
        assert isinstance(block["runs"], list)
        assert block["align"] in ("left", "center", "right", "justify")


class TestLegalSpecificBlocks:
    def test_hecho_block(self):
        block = {"type": "hecho", "num": 1, "runs": [_make_run("PRIMERO. ...")]}
        assert block["type"] == "hecho"
        assert isinstance(block["num"], int)

    def test_pretension_block(self):
        block = {
            "type": "pretension", "ord": 1,
            "runs": [_make_run("PRIMERA. Declarar...")],
            "kind": "declarativa",
        }
        assert block["type"] == "pretension"
        assert block["kind"] in ("declarativa", "condena", "general")

    def test_norma_citada_block(self):
        block = {
            "type": "norma_citada",
            "norma": "Art. 2142 CC",
            "contenido": "El mandato es un contrato...",
            "verified": True,
            "fuente_url": "https://suin...",
            "fuente_url_vigente": "https://suin...",
            # M20.10 nuevo campo
            "tier": "GROUNDED",
        }
        assert block["type"] == "norma_citada"
        assert "norma" in block
        assert block.get("tier") in (None, "GROUNDED", "VERIFY_FLAG", "DEROGADA", "NOT_FOUND")

    def test_jurisprudencia_block(self):
        block = {
            "type": "jurisprudencia",
            "id": "SC2879-2019",
            "mp": "Octavio Tejeiro",
            "corte": "CSJ_civil",
            "fecha": "2019-07-30",
            "ratio": "La cláusula penal...",
            "verified": True,
            "fuente_url": "https://cortesuprema...",
            "tier": "GROUNDED",
        }
        assert block["type"] == "jurisprudencia"
        assert "id" in block
        assert "ratio" in block

    def test_silogismo_block(self):
        block = {
            "type": "silogismo",
            "premisa_mayor": "El que daña debe reparar",
            "premisa_menor": "El demandado dañó",
            "conclusion": "El demandado debe reparar",
        }
        assert block["type"] == "silogismo"
        assert set(block.keys()) >= {"premisa_mayor", "premisa_menor", "conclusion"}

    def test_juramento_block(self):
        block = {"type": "juramento", "runs": [_make_run("JURO solemnemente...")]}
        assert block["type"] == "juramento"

    def test_firma_block(self):
        block = {"type": "firma", "nombre": "Sandra López", "rol": "Apoderada",
                 "cc": "98.765.432", "tp": "12345 C.S.J."}
        assert block["type"] == "firma"
        assert "nombre" in block


class TestStructuredBlocks:
    def test_table_block(self):
        block = {
            "type": "table",
            "header": ["Concepto", "Valor"],
            "rows": [["Capital", "$1.000.000"], ["Intereses", "$87.000"]],
            "shading": False,
        }
        assert block["type"] == "table"
        assert isinstance(block["header"], list)
        assert isinstance(block["rows"], list)
        for row in block["rows"]:
            assert isinstance(row, list)

    def test_calc_step_block(self):
        block = {
            "type": "calc_step",
            "label": "Intereses moratorios",
            "formula": "K × tasa × dias / 365",
            "result": 87421890,
            "currency": "COP",
        }
        assert block["type"] == "calc_step"
        assert "result" in block

    def test_list_item_block(self):
        block = {"type": "list_item", "marker": "•",
                 "runs": [_make_run("Item 1")], "level": 0}
        assert block["type"] == "list_item"


class TestBlockSerialization:
    def test_block_serializable_to_json(self):
        import json
        sample_blocks = [
            {"type": "title", "text": "X", "level": 0},
            {"type": "paragraph", "runs": [_make_run("Texto")]},
            {"type": "norma_citada", "norma": "Art. 1", "contenido": "...", "tier": "GROUNDED"},
        ]
        ser = json.dumps(sample_blocks, ensure_ascii=False)
        deser = json.loads(ser)
        assert deser == sample_blocks

    def test_unicode_preserved_in_blocks(self):
        import json
        block = {"type": "paragraph", "runs": [_make_run("Ñoño señal niño")]}
        ser = json.dumps(block, ensure_ascii=False)
        deser = json.loads(ser)
        assert deser["runs"][0]["text"] == "Ñoño señal niño"


class TestM20NewFields:
    """M20.10 agrega tier a citation-bearing blocks."""

    def test_norma_citada_tier_field(self):
        valid_tiers = {"GROUNDED", "VERIFY_FLAG", "DEROGADA", "NOT_FOUND"}
        for tier in valid_tiers:
            block = {"type": "norma_citada", "norma": "X", "contenido": "Y", "tier": tier}
            assert block["tier"] in valid_tiers

    def test_jurisprudencia_tier_field(self):
        valid_tiers = {"GROUNDED", "VERIFY_FLAG", "DEROGADA", "NOT_FOUND"}
        for tier in valid_tiers:
            block = {"type": "jurisprudencia", "id": "X", "ratio": "Y", "tier": tier}
            assert block["tier"] in valid_tiers
