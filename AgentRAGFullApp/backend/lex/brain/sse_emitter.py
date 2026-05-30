"""Sprint M20.03 · SSE emitter · mapea tool_use a 30 eventos SSE del contrato.

Cada tool_use del Brain produce 1+ eventos SSE en el formato que el frontend
ya espera (sin cambios en el contrato). Esto permite que el LeanOrchestrator
sea drop-in compatible con el Canvas, Chat panel, BriefModal y voice UI.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from lex.blocks.events import SSEEvent, sse

logger = logging.getLogger(__name__)


def map_tool_to_sse_events(
    tool_name: str,
    tool_input: dict,
    tool_result: dict,
    *,
    tool_use_id: str = "",
    iteration: int = 0,
) -> Iterable[bytes]:
    """Mapea un (tool_name, tool_input, tool_result) a los SSE events apropiados.

    Yield 0+ eventos. Siempre yieldea al menos un `agent_thought(kind="tool_call")`
    para que el chat panel refleje la actividad.
    """
    # 1) Siempre: agent_thought con resumen
    summary = _summarize_tool_result(tool_name, tool_input, tool_result)
    yield sse("agent_thought", {
        "id": tool_use_id or f"iter-{iteration}-{tool_name}",
        "type": "tool_call",
        "tool": tool_name,
        "tool_id": tool_use_id,
        "content": summary,
        "confidence": 0.9,
        "kind": "tool_call",
    })

    # 2) Evento específico según la tool
    try:
        if tool_name == "load_skill_md":
            yield from _emit_load_skill_md(tool_input, tool_result)
        elif tool_name == "extract_data":
            yield from _emit_extract_data(tool_input, tool_result)
        elif tool_name == "load_matter_context":
            yield from _emit_load_matter_context(tool_input, tool_result)
        elif tool_name == "verify_citation":
            yield from _emit_verify_citation(tool_input, tool_result)
        elif tool_name == "search_jurisprudence":
            yield from _emit_search_jurisprudence(tool_input, tool_result)
        elif tool_name == "check_derogation":
            yield from _emit_check_derogation(tool_input, tool_result)
        elif tool_name == "calc_legal":
            yield from _emit_calc_legal(tool_input, tool_result)
        elif tool_name == "generate_clause":
            yield from _emit_generate_clause(tool_input, tool_result)
        elif tool_name == "check_completeness":
            yield from _emit_check_completeness(tool_input, tool_result)
        elif tool_name == "check_coherence":
            yield from _emit_check_coherence(tool_input, tool_result)
        elif tool_name == "validate_legal":
            yield from _emit_validate_legal(tool_input, tool_result)
        elif tool_name == "build_docx":
            yield from _emit_build_docx(tool_input, tool_result)
        # narrate_progress y persist_audit no emiten evento extra (ya cubierto por agent_thought)
    except Exception as e:
        logger.warning("sse mapping for %s failed: %s", tool_name, e)


# ---- summary helper ----

def _summarize_tool_result(name: str, inp: dict, out: dict) -> str:
    if not isinstance(out, dict):
        return f"{name}: completado"
    if name == "load_skill_md":
        if out.get("found"):
            return f"SKILL.md cargado: {out.get('doc_type')} ({len(out.get('sections', []))} secciones)"
        return f"SKILL.md no encontrado para {inp.get('doc_type')}"
    if name == "extract_data":
        n_fields = len(out.get("extracted_fields") or {})
        return f"Extraídos {n_fields} campos del intent"
    if name == "verify_citation":
        return f"Cita {inp.get('citation')!r}: {out.get('tier', 'unknown')}"
    if name == "search_jurisprudence":
        n = out.get("count", 0)
        return f"Encontradas {n} sentencias para {inp.get('query')!r}"
    if name == "generate_clause":
        return f"Generada cláusula {inp.get('section_key')!r} ({out.get('block_count', 0)} bloques)"
    if name == "calc_legal":
        return f"Cálculo {inp.get('tipo')!r} completado"
    if name == "check_coherence":
        if out.get("ok"):
            return "Coherencia: OK"
        return f"Coherencia: {out.get('failed_count', 0)} issues"
    if name == "check_completeness":
        return f"Completitud score: {out.get('overall_score', 0):.2f}"
    if name == "build_docx":
        return f"DOCX listo: {out.get('page_count_estimated', '?')} páginas"
    if name == "persist_audit":
        return "Audit persistido"
    if name == "narrate_progress":
        return inp.get("message", "")[:120]
    return f"{name} OK"


# ---- per-tool emitters ----

def _emit_load_skill_md(inp: dict, out: dict):
    if out.get("found"):
        yield SSEEvent.structure_discovered({
            "doc_type": out.get("doc_type"),
            "sections": out.get("sections", []),
            "source": out.get("source"),
        })


def _emit_extract_data(inp: dict, out: dict):
    yield SSEEvent.extraction_done(
        extracted_fields=out.get("extracted_fields") or {},
        missing_fields=out.get("missing_fields") or [],
    )


def _emit_load_matter_context(inp: dict, out: dict):
    yield sse("matter_context_loaded", {
        "matter_id": inp.get("matter_id"),
        "parties_count": len(out.get("parties") or []),
        "deadlines_count": len(out.get("deadlines") or []),
        "risks_count": len(out.get("risks") or []),
        "documents_count": len(out.get("documents") or []),
    })


def _emit_verify_citation(inp: dict, out: dict):
    yield SSEEvent.citation_verify(
        citation=inp.get("citation", ""),
        found=bool(out.get("exists")),
        chunk_id=None,
        similarity=out.get("confidence"),
    )


def _emit_search_jurisprudence(inp: dict, out: dict):
    yield SSEEvent.jurisprudence_query(
        query=inp.get("query", ""),
        hits=out.get("hits") or [],
        hunter="lean_brain",
    )


def _emit_check_derogation(inp: dict, out: dict):
    yield SSEEvent.derogation_check(
        norma=inp.get("norma_text", ""),
        vigente=bool(out.get("vigente")),
        derogada_por=out.get("derogada_por"),
    )


def _emit_calc_legal(inp: dict, out: dict):
    yield SSEEvent.calculation_done(
        conceptos={inp.get("tipo", "calc"): out.get("result")},
        total=None,
    )


def _emit_generate_clause(inp: dict, out: dict):
    section_key = inp.get("section_key", "default")
    blocks = out.get("blocks") or []
    yield SSEEvent.section_started(
        section_key=section_key,
        order=inp.get("section_order", 1),
        total=1,
    )
    for b in blocks:
        yield SSEEvent.block_emit(section_key=section_key, block=b)
    yield SSEEvent.section_done(section_key=section_key)


def _emit_check_completeness(inp: dict, out: dict):
    yield SSEEvent.completeness_check_done({
        "ok": out.get("ok"),
        "overall_score": out.get("overall_score"),
        "missing_fields": out.get("missing_fields") or [],
    })
    if out.get("missing_fields"):
        yield SSEEvent.missing_data({"fields": out["missing_fields"]})


def _emit_check_coherence(inp: dict, out: dict):
    yield SSEEvent.coherence_check_done({
        "ok": out.get("ok"),
        "overall_score": out.get("overall_score"),
        "failed_gates": out.get("failed_gates") or [],
    })


def _emit_validate_legal(inp: dict, out: dict):
    yield SSEEvent.legal_classification(out.get("classification") or {})
    yield SSEEvent.qa_done(
        passed=bool(out.get("passed", True)),
        score=float(out.get("qa", {}).get("score", 7.5)),
        issues=out.get("qa", {}).get("issues") or [],
    )


def _emit_build_docx(inp: dict, out: dict):
    if out.get("download_url"):
        yield SSEEvent.presented_file(
            name=f"{inp.get('title', 'documento')}.docx",
            url=out["download_url"],
            size_kb=out.get("size_kb"),
        )
    yield SSEEvent.docx_built(
        url=out.get("download_url") or "",
        size_kb=int(out.get("size_kb") or 0),
    )
