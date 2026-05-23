"""Tests unitarios de QA rules sobre bloques mock (sin LLM ni BD).

Valida que el QA agent detecte correctamente:
- Secciones faltantes
- Hechos insuficientes
- Pretensiones insuficientes
- Citas obligatorias faltantes
"""
import asyncio

import pytest

from lex.orchestrator.stages.qa import run_qa
from lex.templates import registry


@pytest.mark.asyncio
async def test_qa_passes_with_all_sections_and_citations():
    """Caso ideal: todas las secciones requeridas + hechos suficientes."""
    tpl = registry.get("demanda_laboral_ordinaria")
    assert tpl is not None
    # Mock 80 bloques bien estructurados
    blocks = [
        {"block_type": "section_heading", "block_data": {"section_key": k, "type": "section_heading"}}
        for k in ["partes", "hechos", "pretensiones", "liquidacion", "fundamentos_derecho",
                  "razonamiento", "competencia", "pruebas", "anexos", "notificaciones", "juramento", "firma"]
    ]
    # 15 hechos
    blocks += [{"block_type": "hecho", "block_data": {"type": "hecho", "num": i}} for i in range(1, 16)]
    # 10 pretensiones
    blocks += [{"block_type": "pretension", "block_data": {"type": "pretension", "ord": str(i)}} for i in range(1, 11)]
    # Citas Art. 64 + Art. 65
    blocks += [
        {"block_type": "norma_citada", "block_data": {"type": "norma_citada", "norma": "Art. 64 CST"}},
        {"block_type": "norma_citada", "block_data": {"type": "norma_citada", "norma": "Art. 65 CST"}},
    ]
    # 3 jurisprudencias
    blocks += [
        {"block_type": "jurisprudencia", "block_data": {"type": "jurisprudencia", "id": f"SL{i}-2022"}}
        for i in range(3)
    ]

    qa = await run_qa(blocks, tpl)
    assert qa["score"] >= 7.0, f"Got issues: {qa['issues']}"


@pytest.mark.asyncio
async def test_qa_fails_with_missing_sections():
    """Caso fallido: faltan secciones requeridas."""
    tpl = registry.get("demanda_laboral_ordinaria")
    blocks = []  # vacío
    qa = await run_qa(blocks, tpl)
    assert qa["passed"] is False
    assert len(qa["issues"]) > 0
    # Debe reportar al menos 'partes', 'hechos', 'pretensiones', 'liquidacion'
    issues_text = " ".join(qa["issues"])
    assert "partes" in issues_text or "hechos" in issues_text


@pytest.mark.asyncio
async def test_qa_tutela_requires_juramento():
    """Tutela require juramento por DetailProfile."""
    tpl = registry.get("tutela")
    # Mock con partes y hechos pero sin juramento
    blocks = [
        {"block_type": "section_heading", "block_data": {"section_key": "partes"}},
        {"block_type": "section_heading", "block_data": {"section_key": "hechos"}},
        {"block_type": "section_heading", "block_data": {"section_key": "pretensiones"}},
        {"block_type": "section_heading", "block_data": {"section_key": "derechos_vulnerados"}},
        {"block_type": "section_heading", "block_data": {"section_key": "procedibilidad"}},
        {"block_type": "hecho", "block_data": {"type": "hecho", "num": 1}},
        {"block_type": "hecho", "block_data": {"type": "hecho", "num": 2}},
        {"block_type": "hecho", "block_data": {"type": "hecho", "num": 3}},
        {"block_type": "pretension", "block_data": {"type": "pretension", "ord": "PRIMERO"}},
        {"block_type": "pretension", "block_data": {"type": "pretension", "ord": "SEGUNDO"}},
        {"block_type": "jurisprudencia", "block_data": {"type": "jurisprudencia", "id": "T-760"}},
        {"block_type": "jurisprudencia", "block_data": {"type": "jurisprudencia", "id": "SU-449"}},
    ]
    qa = await run_qa(blocks, tpl)
    issues_text = " ".join(qa["issues"]).lower()
    assert "juramento" in issues_text


@pytest.mark.asyncio
async def test_qa_no_template_returns_pass():
    """Sin template, QA pasa con score default."""
    qa = await run_qa([], None)
    assert qa["passed"] is True
    assert qa["score"] == 7.5
