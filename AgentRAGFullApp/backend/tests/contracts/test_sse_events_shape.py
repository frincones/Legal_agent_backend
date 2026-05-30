"""Sprint M20.01 · Tests de contrato · SSE events shape.

Congela el shape de cada SSEEvent (nombre + keys obligatorias).
Si cambia el contrato, el frontend rompe → este test debe romper antes.

Cobertura:
  - 30+ eventos del pipeline actual (M19.31)
  - shapes esperados por useDocumentGenStream y useSSEHandlers en frontend
"""
from __future__ import annotations

import json
import re

import pytest

from lex.blocks.events import SSEEvent, sse, keepalive


def _parse_sse_bytes(data: bytes) -> tuple[str, dict]:
    """Parsea bytes SSE → (event_name, data_dict)."""
    text = data.decode("utf-8")
    m = re.search(r"event: (\S+)\ndata: (.+)\n\n", text, re.DOTALL)
    if not m:
        raise AssertionError(f"SSE mal formado: {text!r}")
    return m.group(1), json.loads(m.group(2))


class TestPipelineLifecycle:
    """Eventos del ciclo de vida del pipeline (meta, classification, etc.)."""

    def test_meta_event(self):
        data = SSEEvent.meta(
            generation_id="g-1",
            template_selected={"id": "poder_especial", "version": 1},
            sections_plan=[{"key": "objeto", "order": 1}],
            estimated_seconds=60,
        )
        name, payload = _parse_sse_bytes(data)
        assert name == "meta"
        assert payload["generation_id"] == "g-1"
        assert "template_selected" in payload
        assert "sections_plan" in payload
        assert "estimated_seconds" in payload

    def test_classification_done_event(self):
        data = SSEEvent.classification_done(
            doc_type="poder_especial", jurisdiccion="CO",
            materia="notarial", confidence=0.95,
        )
        name, payload = _parse_sse_bytes(data)
        assert name == "classification_done"
        assert set(payload.keys()) >= {"doc_type", "jurisdiccion", "materia", "confidence"}

    def test_extraction_done_event(self):
        data = SSEEvent.extraction_done(
            extracted_fields={"poderdante": "Juan"}, missing_fields=[],
        )
        name, payload = _parse_sse_bytes(data)
        assert name == "extraction_done"
        assert "extracted_fields" in payload
        assert "missing_fields" in payload

    def test_calculation_done_event(self):
        data = SSEEvent.calculation_done(conceptos={"intereses": 1000}, total=1000)
        name, payload = _parse_sse_bytes(data)
        assert name == "calculation_done"
        assert "conceptos" in payload
        assert "total" in payload


class TestHuntersAndJurisprudence:
    def test_jurisprudence_query_event(self):
        data = SSEEvent.jurisprudence_query(
            query="perjuicios morales",
            hits=[{"id": "SC-1", "score": 0.9}],
            hunter="sala_civil_csj",
        )
        name, payload = _parse_sse_bytes(data)
        assert name == "jurisprudence_query"
        assert set(payload.keys()) >= {"query", "hits", "hunter"}

    def test_derogation_check_event(self):
        data = SSEEvent.derogation_check(
            norma="Ley 153 de 1887", vigente=True, derogada_por=None,
        )
        name, payload = _parse_sse_bytes(data)
        assert name == "derogation_check"
        assert set(payload.keys()) >= {"norma", "vigente", "derogada_por"}


class TestBlockStreaming:
    """Eventos de streaming de bloques (clave para el Canvas TipTap)."""

    def test_section_started_event(self):
        data = SSEEvent.section_started(section_key="objeto", order=1, total=6)
        name, payload = _parse_sse_bytes(data)
        assert name == "section_started"
        assert payload["section_key"] == "objeto"
        assert payload["order"] == 1
        assert payload["total_sections"] == 6

    def test_block_emit_event(self):
        block = {"type": "paragraph", "runs": [{"text": "Hola"}]}
        data = SSEEvent.block_emit(section_key="objeto", block=block)
        name, payload = _parse_sse_bytes(data)
        assert name == "block_emit"
        assert payload["section_key"] == "objeto"
        assert payload["block"] == block

    def test_block_streaming_event(self):
        data = SSEEvent.block_streaming(block_id="b-1", run_delta={"text": "ho"})
        name, payload = _parse_sse_bytes(data)
        assert name == "block_streaming"
        assert "block_id" in payload
        assert "run_delta" in payload

    def test_block_done_event(self):
        data = SSEEvent.block_done(block_id="b-1")
        name, payload = _parse_sse_bytes(data)
        assert name == "block_done"
        assert payload["block_id"] == "b-1"

    def test_section_done_event(self):
        data = SSEEvent.section_done(section_key="objeto")
        name, payload = _parse_sse_bytes(data)
        assert name == "section_done"
        assert payload["section_key"] == "objeto"


class TestVerification:
    def test_citation_verify_event(self):
        data = SSEEvent.citation_verify(
            citation="Art. 2142 CC", found=True,
            chunk_id="ch-1", similarity=0.95,
        )
        name, payload = _parse_sse_bytes(data)
        assert name == "citation_verify"
        assert set(payload.keys()) >= {"citation", "found", "chunk_id", "similarity"}


class TestPolishQA:
    def test_polish_done_event(self):
        data = SSEEvent.polish_done(polished_chars=1000, delta_chars=50)
        name, payload = _parse_sse_bytes(data)
        assert name == "polish_done"
        assert "polished_chars" in payload
        assert "delta_chars" in payload

    def test_polish_done_with_error(self):
        data = SSEEvent.polish_done(polished_chars=1000, error="timeout")
        name, payload = _parse_sse_bytes(data)
        assert payload.get("error") == "timeout"

    def test_qa_done_event(self):
        data = SSEEvent.qa_done(passed=True, score=0.92, issues=[])
        name, payload = _parse_sse_bytes(data)
        assert name == "qa_done"
        assert set(payload.keys()) >= {"passed", "score", "issues"}


class TestDocxAndAudit:
    def test_docx_built_event(self):
        data = SSEEvent.docx_built(url="https://signed.url/doc.docx", size_kb=187)
        name, payload = _parse_sse_bytes(data)
        assert name == "docx_built"
        assert "url" in payload
        assert "size_kb" in payload

    def test_audit_report_event(self):
        audit = {"total_seconds": 30, "cost_usd": 0.05}
        data = SSEEvent.audit_report(audit)
        name, payload = _parse_sse_bytes(data)
        assert name == "audit_report"
        assert payload == audit


class TestM19LegalEnrichment:
    """Eventos de los sprints M19 (context enrichment, legal classification, etc.)."""

    def test_context_enrichment_started(self):
        data = SSEEvent.context_enrichment_started()
        name, payload = _parse_sse_bytes(data)
        assert name == "context_enrichment_started"

    def test_context_enrichment_done(self):
        data = SSEEvent.context_enrichment_done({"hits": 3})
        name, payload = _parse_sse_bytes(data)
        assert name == "context_enrichment_done"
        assert payload == {"hits": 3}

    def test_legal_classification(self):
        data = SSEEvent.legal_classification({"regimen": "civil", "naturaleza": "contractual"})
        name, payload = _parse_sse_bytes(data)
        assert name == "legal_classification"
        assert "regimen" in payload

    def test_risk_advisory(self):
        data = SSEEvent.risk_advisory({"falta": "X", "consecuencia": "Y", "recomendacion": "Z"})
        name, payload = _parse_sse_bytes(data)
        assert name == "risk_advisory"
        assert set(payload.keys()) >= {"falta", "consecuencia", "recomendacion"}

    def test_structure_discovered(self):
        data = SSEEvent.structure_discovered({"recipe_id": "r-1", "sections": []})
        name, payload = _parse_sse_bytes(data)
        assert name == "structure_discovered"
        assert "recipe_id" in payload

    def test_missing_data(self):
        data = SSEEvent.missing_data({"fields": ["NIT"]})
        name, payload = _parse_sse_bytes(data)
        assert name == "missing_data"
        assert "fields" in payload

    def test_missing_data_resolved(self):
        data = SSEEvent.missing_data_resolved({"NIT": "900.xxx"})
        name, payload = _parse_sse_bytes(data)
        assert name == "missing_data_resolved"
        assert "filled_fields" in payload


class TestQualityLoop:
    def test_completeness_check_done(self):
        data = SSEEvent.completeness_check_done({"ok": True})
        name, payload = _parse_sse_bytes(data)
        assert name == "completeness_check_done"

    def test_coherence_check_done(self):
        data = SSEEvent.coherence_check_done({"ok": True, "issues": []})
        name, payload = _parse_sse_bytes(data)
        assert name == "coherence_check_done"

    def test_quality_report(self):
        data = SSEEvent.quality_report({"score": 0.91})
        name, payload = _parse_sse_bytes(data)
        assert name == "quality_report"

    def test_autoloop_iteration(self):
        data = SSEEvent.autoloop_iteration(iteration=2, gaps_remaining=1,
                                            regenerating_sections=["pretensiones"])
        name, payload = _parse_sse_bytes(data)
        assert name == "autoloop_iteration"
        assert set(payload.keys()) >= {"iteration", "gaps_remaining", "regenerating_sections"}


class TestTerminalEvents:
    def test_done_event(self):
        data = SSEEvent.done(
            generation_id="g-1", matter_document_id="d-1",
            duration_seconds=30.5, cost_usd=0.06, total_blocks=8,
        )
        name, payload = _parse_sse_bytes(data)
        assert name == "done"
        assert set(payload.keys()) >= {
            "generation_id", "matter_document_id",
            "duration_seconds", "cost_usd", "total_blocks",
        }

    def test_error_event(self):
        data = SSEEvent.error(stage="block_generator", message="timeout")
        name, payload = _parse_sse_bytes(data)
        assert name == "error"
        assert payload["stage"] == "block_generator"
        assert payload["message"] == "timeout"


class TestM19_30StageProgress:
    def test_stage_progress_running(self):
        data = SSEEvent.stage_progress(stage="classifier", state="running",
                                        label="Clasificando…", elapsed_ms=500)
        name, payload = _parse_sse_bytes(data)
        assert name == "stage_progress"
        assert payload["stage"] == "classifier"
        assert payload["state"] == "running"
        assert payload["label"] == "Clasificando…"

    def test_stage_progress_states(self):
        for state in ("running", "ok", "skipped", "timeout", "error", "fallback"):
            data = SSEEvent.stage_progress(stage="x", state=state)
            _, payload = _parse_sse_bytes(data)
            assert payload["state"] == state


class TestPresentedFile:
    def test_presented_file_minimal(self):
        data = SSEEvent.presented_file(name="doc.docx")
        name, payload = _parse_sse_bytes(data)
        assert name == "presented_file"
        assert payload["name"] == "doc.docx"

    def test_presented_file_complete(self):
        data = SSEEvent.presented_file(
            name="doc.docx", url="https://signed.url",
            size_kb=187, preview_b64="iVBOR…", thread_id="t-1",
        )
        _, payload = _parse_sse_bytes(data)
        assert payload["url"] == "https://signed.url"
        assert payload["size_kb"] == 187


class TestPrimitives:
    def test_sse_helper_format(self):
        data = sse("custom_event", {"key": "value"})
        text = data.decode("utf-8")
        assert text.startswith("event: custom_event\ndata: ")
        assert text.endswith("\n\n")

    def test_keepalive(self):
        data = keepalive()
        assert data == b": keepalive\n\n"

    def test_unicode_preserved(self):
        data = sse("test", {"text": "Tildes ñoño y acentós"})
        _, payload = _parse_sse_bytes(data)
        assert payload["text"] == "Tildes ñoño y acentós"


class TestM20FutureEvents:
    """Eventos que el nuevo LeanOrchestrator (M20+) emitirá.
    Por ahora marcados como expected pero no implementados.
    Cuando se agreguen al SSEEvent, descomentar y deben pasar.
    """

    @pytest.mark.skip(reason="M20.02 LeanOrchestrator emite agent_thought con tool_call")
    def test_agent_thought_tool_call(self):
        # esperado: SSEEvent.agent_thought(message="Llamando verify_citation",
        #                                  kind="tool_call", tool="verify_citation",
        #                                  tool_id="tu-1", tool_response={...})
        pass

    @pytest.mark.skip(reason="M20.10 4-tier citation markers")
    def test_citation_verify_with_tier(self):
        # esperado: SSEEvent.citation_verify(..., tier="GROUNDED|VERIFY_FLAG|DEROGADA|NOT_FOUND")
        pass
