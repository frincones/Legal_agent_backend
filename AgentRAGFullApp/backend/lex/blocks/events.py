"""SSE event factories tipadas para Document Generation v3.1.

Cada función devuelve bytes (la línea SSE formateada listo para yield).
Los nombres de evento son contrato fijo con el frontend.
"""
from __future__ import annotations

import json
from typing import Any, Literal

EventName = Literal[
    # Pipeline lifecycle
    "meta",
    "classification_started",
    "classification_done",
    "template_loaded",
    "extraction_started",
    "extraction_done",
    "calculation_started",
    "calculation_done",
    "hunters_started",
    "jurisprudence_query",
    "hunters_done",
    "derogation_started",
    "derogation_check",
    "derogation_done",
    # Block streaming
    "section_started",
    "block_emit",
    "block_streaming",
    "block_done",
    "section_done",
    # Verification
    "citation_verify_started",
    "citation_verify",
    "citation_verify_done",
    # Polish + QA
    "polish_started",
    "polish_done",
    "qa_started",
    "qa_done",
    # Docx
    "docx_built",
    # Final
    "audit_report",
    "done",
    "error",
    # M18.d: Agent thought stream — narración en vivo estilo Claude
    "agent_thought",
    # M19.7: archivo final presentado (DOCX/PDF) con preview
    "presented_file",
]


def sse(event: EventName, data: Any) -> bytes:
    """Formatea un evento SSE como bytes listos para yield."""
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


def keepalive() -> bytes:
    """Línea keepalive para evitar timeout de proxies."""
    return b": keepalive\n\n"


class SSEEvent:
    """Helper class para emitir eventos con estructura consistente."""

    @staticmethod
    def meta(generation_id: str, template_selected: dict, sections_plan: list[dict],
             estimated_seconds: int) -> bytes:
        return sse("meta", {
            "generation_id": generation_id,
            "template_selected": template_selected,
            "sections_plan": sections_plan,
            "estimated_seconds": estimated_seconds,
        })

    @staticmethod
    def classification_done(doc_type: str, jurisdiccion: str, materia: str,
                            confidence: float) -> bytes:
        return sse("classification_done", {
            "doc_type": doc_type,
            "jurisdiccion": jurisdiccion,
            "materia": materia,
            "confidence": confidence,
        })

    @staticmethod
    def extraction_done(extracted_fields: dict, missing_fields: list[str]) -> bytes:
        return sse("extraction_done", {
            "extracted_fields": extracted_fields,
            "missing_fields": missing_fields,
        })

    @staticmethod
    def calculation_done(conceptos: dict, total: float | None = None) -> bytes:
        return sse("calculation_done", {
            "conceptos": conceptos,
            "total": total,
        })

    @staticmethod
    def jurisprudence_query(query: str, hits: list[dict], hunter: str) -> bytes:
        return sse("jurisprudence_query", {
            "query": query,
            "hits": hits,
            "hunter": hunter,
        })

    @staticmethod
    def derogation_check(norma: str, vigente: bool, derogada_por: str | None = None) -> bytes:
        return sse("derogation_check", {
            "norma": norma,
            "vigente": vigente,
            "derogada_por": derogada_por,
        })

    @staticmethod
    def section_started(section_key: str, order: int, total: int) -> bytes:
        return sse("section_started", {
            "section_key": section_key,
            "order": order,
            "total_sections": total,
        })

    @staticmethod
    def block_emit(section_key: str, block: dict) -> bytes:
        return sse("block_emit", {
            "section_key": section_key,
            "block": block,
        })

    @staticmethod
    def block_streaming(block_id: str, run_delta: dict) -> bytes:
        return sse("block_streaming", {
            "block_id": block_id,
            "run_delta": run_delta,
        })

    @staticmethod
    def block_done(block_id: str) -> bytes:
        return sse("block_done", {"block_id": block_id})

    @staticmethod
    def section_done(section_key: str) -> bytes:
        return sse("section_done", {"section_key": section_key})

    @staticmethod
    def citation_verify(citation: str, found: bool, chunk_id: str | None = None,
                        similarity: float | None = None) -> bytes:
        return sse("citation_verify", {
            "citation": citation,
            "found": found,
            "chunk_id": chunk_id,
            "similarity": similarity,
        })

    @staticmethod
    def polish_started(model: str, draft_chars: int) -> bytes:
        return sse("polish_started", {"model": model, "draft_chars": draft_chars})

    @staticmethod
    def polish_done(polished_chars: int, delta_chars: int = 0,
                    error: str | None = None) -> bytes:
        payload = {"polished_chars": polished_chars, "delta_chars": delta_chars}
        if error:
            payload["error"] = error
        return sse("polish_done", payload)

    @staticmethod
    def qa_done(passed: bool, score: float, issues: list[str]) -> bytes:
        return sse("qa_done", {"passed": passed, "score": score, "issues": issues})

    @staticmethod
    def docx_built(url: str, size_kb: int) -> bytes:
        return sse("docx_built", {"url": url, "size_kb": size_kb})

    @staticmethod
    def audit_report(audit: dict) -> bytes:
        return sse("audit_report", audit)

    # M19.22 — Context Enrichment (pre-research estilo Claude)
    @staticmethod
    def context_enrichment_started() -> bytes:
        return sse("context_enrichment_started", {})

    @staticmethod
    def context_enrichment_done(report: dict) -> bytes:
        return sse("context_enrichment_done", report)

    # M19.23 — Structure Discovery + Data Completeness Gate
    @staticmethod
    def structure_discovered(recipe: dict) -> bytes:
        """Emitido tras structure_discovery — informa la estructura elegida."""
        return sse("structure_discovered", recipe)

    @staticmethod
    def missing_data(report: dict) -> bytes:
        """Emitido cuando faltan datos críticos. En modo borrador es solo
        informativo (pipeline sigue). En modo firma pausa la generación
        esperando POST /documents/v2/resume-generation."""
        return sse("missing_data", report)

    @staticmethod
    def missing_data_resolved(filled_fields: dict) -> bytes:
        """Emitido tras /resume-generation con los datos que el usuario completó."""
        return sse("missing_data_resolved", {"filled_fields": filled_fields})

    # M19.20 — Quality Loop Continuo
    @staticmethod
    def completeness_check_done(report: dict) -> bytes:
        return sse("completeness_check_done", report)

    @staticmethod
    def coherence_check_done(report: dict) -> bytes:
        return sse("coherence_check_done", report)

    @staticmethod
    def quality_report(report: dict) -> bytes:
        return sse("quality_report", report)

    @staticmethod
    def autoloop_iteration(iteration: int, gaps_remaining: int, regenerating_sections: list[str]) -> bytes:
        return sse("autoloop_iteration", {
            "iteration": iteration,
            "gaps_remaining": gaps_remaining,
            "regenerating_sections": regenerating_sections,
        })

    @staticmethod
    def done(generation_id: str, matter_document_id: str | None,
             duration_seconds: float, cost_usd: float, total_blocks: int) -> bytes:
        return sse("done", {
            "generation_id": generation_id,
            "matter_document_id": matter_document_id,
            "duration_seconds": duration_seconds,
            "cost_usd": cost_usd,
            "total_blocks": total_blocks,
        })

    @staticmethod
    def error(stage: str, message: str) -> bytes:
        return sse("error", {"stage": stage, "message": message})

    @staticmethod
    def presented_file(
        name: str,
        url: str | None = None,
        size_kb: int | None = None,
        mime: str = "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        preview_b64: str | None = None,
        thread_id: str | None = None,
    ) -> bytes:
        """M19.7: presenta un archivo generado (DOCX) con preview opcional.

        El frontend renderiza como PresentedFileChip dentro del thread del agente.
        """
        return sse("presented_file", {
            "name": name,
            "url": url,
            "size_kb": size_kb,
            "mime": mime,
            "preview_b64": preview_b64,
            "thread_id": thread_id,
        })

    @staticmethod
    def agent_thought(
        message: str,
        kind: str = "info",          # narration|tool_call|tool_result|correction|warning|success|info|error
        tool: str | None = None,     # 'brave_search'|'judge'|'corte_cc'|...
        ref: str | None = None,      # citation_ref si aplica
        url: str | None = None,      # URL relevante
        suggestion: str | None = None,  # correccion propuesta
        # M19.5: capacidad estilo Claude
        tool_id: str | None = None,             # id único del tool call (para correlar request/response)
        tool_request: dict | None = None,       # JSON args enviados al tool
        tool_response: dict | None = None,      # JSON respuesta del tool
        tool_error: str | None = None,          # error si tool falló
        tool_duration_ms: int | None = None,    # latencia ejecución
        thread_id: str | None = None,           # agrupa thoughts en mismo "mensaje" del asistente
    ) -> bytes:
        """M18.d + M19.5: narración del agente en vivo estilo Claude.

        Tipos de `kind`:
          - "narration"  : párrafo de prosa (markdown) del agente
          - "tool_call"  : invocación de tool con request/response
          - "tool_result": resultado de tool (legacy, prefer tool_call con response)
          - "correction" : sugerencia de corrección legal
          - "warning"    : nota legal importante
          - "success"    : hito completado
          - "info"       : log genérico (legacy, prefer "narration")
          - "error"      : algo falló
        """
        return sse("agent_thought", {
            "kind": kind,
            "message": message[:2000] if message else "",
            "tool": tool,
            "ref": ref,
            "url": url,
            "suggestion": suggestion,
            "tool_id": tool_id,
            "tool_request": tool_request,
            "tool_response": tool_response,
            "tool_error": tool_error,
            "tool_duration_ms": tool_duration_ms,
            "thread_id": thread_id,
        })
