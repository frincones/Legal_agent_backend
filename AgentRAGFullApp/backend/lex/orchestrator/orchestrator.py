"""Orchestrator principal del pipeline de Document Generation v3.1.

Pipeline (M1, mínimo viable):
  1. classifier      → detecta doc_type
  2. data_extractor  → extrae datos del brief
  3. (M3) calculadora → cálculos puros si aplica
  4. (M3) hunters     → RAG multi-query
  5. (M4) derogation  → verifier de vigencia
  6. block_generator → genera Block[] por sección
  7. (M4) citation_verifier
  8. (M5) polish
  9. (M5) qa_agent
  10. (M5) docx_builder

En M1 implementamos solo classifier + extractor + block_generator + persistencia.
Cada stage emite eventos SSE tipados.

Stages futuros (M3-M5) se insertan como hooks plugables.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from lex.blocks import SSEEvent, sse
from lex.blocks.schema import (
    Block,
    FirmaBlock,
    JuramentoBlock,
    SectionHeadingBlock,
    TitleBlock,
)
from lex.orchestrator.stages.block_generator import generate_section_blocks
from lex.orchestrator.stages.calculadora import run_calculadora
from lex.orchestrator.stages.classifier import classify
from lex.orchestrator.stages.data_extractor import extract
from lex.orchestrator.stages.hunters_stage import run_template_hunters
from lex.storage import AuditRepo, BlocksRepo

logger = logging.getLogger(__name__)


# Plan estático por doc_type — M2 lo reemplazará con TemplateDef
# (mismo plan que documents_generate.py v1, pero usando section_key estable)
SECTIONS_PLAN_BY_TYPE: dict[str, list[dict[str, Any]]] = {
    "demanda_laboral_ordinaria": [
        {"key": "encabezado", "title": "Encabezado y referencia", "order": 1, "roman": None},
        {"key": "partes", "title": "PARTES DEL PROCESO", "order": 2, "roman": "I"},
        {"key": "competencia", "title": "JURISDICCIÓN, COMPETENCIA Y CUANTÍA", "order": 3, "roman": "II"},
        {"key": "hechos", "title": "HECHOS", "order": 4, "roman": "III"},
        {"key": "pretensiones", "title": "PRETENSIONES", "order": 5, "roman": "IV"},
        {"key": "fundamentos_derecho", "title": "FUNDAMENTOS DE DERECHO", "order": 6, "roman": "V"},
        {"key": "razonamiento", "title": "RAZONAMIENTO JURÍDICO", "order": 7, "roman": "VI"},
        {"key": "liquidacion", "title": "LIQUIDACIÓN DE LAS PRETENSIONES", "order": 8, "roman": "VII"},
        {"key": "pruebas", "title": "PRUEBAS", "order": 9, "roman": "VIII"},
        {"key": "anexos", "title": "ANEXOS", "order": 10, "roman": "IX"},
        {"key": "notificaciones", "title": "NOTIFICACIONES", "order": 11, "roman": "X"},
        {"key": "juramento", "title": "JURAMENTO", "order": 12, "roman": None},
        {"key": "firma", "title": "Firma", "order": 13, "roman": None},
    ],
    "tutela": [
        {"key": "encabezado", "title": "Encabezado", "order": 1, "roman": None},
        {"key": "partes", "title": "PARTES", "order": 2, "roman": "I"},
        {"key": "hechos", "title": "HECHOS", "order": 3, "roman": "II"},
        {"key": "derechos_vulnerados", "title": "DERECHOS FUNDAMENTALES VULNERADOS", "order": 4, "roman": "III"},
        {"key": "pretensiones", "title": "PRETENSIONES", "order": 5, "roman": "IV"},
        {"key": "fundamentos_derecho", "title": "FUNDAMENTOS DE DERECHO", "order": 6, "roman": "V"},
        {"key": "pruebas", "title": "PRUEBAS", "order": 7, "roman": "VI"},
        {"key": "juramento", "title": "JURAMENTO (Art. 37 D. 2591/91)", "order": 8, "roman": None},
        {"key": "firma", "title": "Firma", "order": 9, "roman": None},
    ],
    "contrato_arrendamiento": [
        {"key": "encabezado", "title": "Encabezado", "order": 1, "roman": None},
        {"key": "partes", "title": "PARTES", "order": 2, "roman": "I"},
        {"key": "objeto", "title": "OBJETO DEL CONTRATO", "order": 3, "roman": "II"},
        {"key": "canon", "title": "CANON Y FORMA DE PAGO", "order": 4, "roman": "III"},
        {"key": "duracion", "title": "DURACIÓN Y PRÓRROGA", "order": 5, "roman": "IV"},
        {"key": "obligaciones", "title": "OBLIGACIONES DE LAS PARTES", "order": 6, "roman": "V"},
        {"key": "terminacion", "title": "CAUSALES DE TERMINACIÓN", "order": 7, "roman": "VI"},
        {"key": "firma", "title": "Firmas", "order": 8, "roman": None},
    ],
    "derecho_peticion": [
        {"key": "encabezado", "title": "Encabezado", "order": 1, "roman": None},
        {"key": "peticionario", "title": "PETICIONARIO", "order": 2, "roman": "I"},
        {"key": "hechos", "title": "HECHOS", "order": 3, "roman": "II"},
        {"key": "peticion", "title": "PETICIÓN CONCRETA", "order": 4, "roman": "III"},
        {"key": "fundamentos_derecho", "title": "FUNDAMENTOS DE DERECHO", "order": 5, "roman": "IV"},
        {"key": "notificaciones", "title": "NOTIFICACIONES", "order": 6, "roman": "V"},
        {"key": "firma", "title": "Firma", "order": 7, "roman": None},
    ],
}


@dataclass
class GenerationRequest:
    intent: str
    user_brief: str = ""
    matter_id: str | None = None
    firm_id: str | None = None
    materia: str | None = None
    doc_type: str | None = None
    context: dict[str, Any] = field(default_factory=dict)


def _resolve_plan(doc_type: str) -> list[dict[str, Any]]:
    """Resuelve plan desde TemplateDef registry (M2) con fallback al hardcoded (M1)."""
    # M2: usar registry de TemplateDef
    try:
        from lex.templates import registry as _tpl_registry
        tpl = _tpl_registry.get(doc_type)
        if tpl is not None:
            return [
                {"key": s.key, "title": s.title, "order": s.order, "roman": s.roman}
                for s in tpl.sections_plan
            ]
    except Exception:
        logger.exception("template registry lookup failed for %s", doc_type)

    # Fallback M1 hardcoded
    if doc_type in SECTIONS_PLAN_BY_TYPE:
        return SECTIONS_PLAN_BY_TYPE[doc_type]
    if doc_type.startswith("demanda_"):
        return SECTIONS_PLAN_BY_TYPE["demanda_laboral_ordinaria"]
    if doc_type.startswith("contrato_"):
        return SECTIONS_PLAN_BY_TYPE["contrato_arrendamiento"]
    return SECTIONS_PLAN_BY_TYPE["tutela"]


def _resolve_template(doc_type: str):
    """Devuelve el TemplateDef si existe."""
    try:
        from lex.templates import registry as _tpl_registry
        return _tpl_registry.get(doc_type)
    except Exception:
        return None


class Orchestrator:
    """Pipeline orchestrator que emite eventos SSE."""

    def __init__(self, openai_client, pool=None):
        self.client = openai_client
        self.pool = pool
        self.blocks_repo = BlocksRepo(pool) if pool else None
        self.audit_repo = AuditRepo(pool) if pool else None

    async def _log_shadow_diffs(
        self,
        legacy_results: list[dict],
        agent_results: list[dict],
        generation_id: str,
    ) -> None:
        """M14: registra divergencias entre legacy y nuevo agent en shadow mode."""
        if not self.pool or not legacy_results or not agent_results:
            return
        try:
            import json as _json
            # Indexar por ref+type
            legacy_idx = {(r.get("ref"), r.get("type")): r for r in legacy_results}
            agent_idx = {(r.get("ref"), r.get("type")): r for r in agent_results}
            keys = set(legacy_idx) | set(agent_idx)

            async with self.pool.acquire() as conn:
                for k in keys:
                    legacy = legacy_idx.get(k, {})
                    agent = agent_idx.get(k, {})
                    legacy_state = legacy.get("estado", "missing")
                    agent_state = agent.get("estado", "missing")

                    if legacy_state == agent_state:
                        diff_type = "identical"
                        is_critical = False
                    elif (legacy_state == "verificada" and agent_state == "no_encontrada") or \
                         (legacy_state == "no_encontrada" and agent_state == "verificada"):
                        diff_type = "critical"
                        is_critical = True
                    elif "verificada" in (legacy_state, agent_state):
                        diff_type = "medium"
                        is_critical = False
                    else:
                        diff_type = "minor"
                        is_critical = False

                    if diff_type == "identical":
                        continue  # no loggear los iguales

                    await conn.execute(
                        """
                        INSERT INTO verification_shadow_diffs
                          (generation_id, citation_ref, citation_type,
                           legacy_state, legacy_method, legacy_fuente_url,
                           agent_state, agent_method, agent_confidence,
                           agent_fuente_url, is_critical, diff_type)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                        """,
                        uuid.UUID(generation_id) if generation_id else None,
                        k[0] or "",
                        k[1] or "norma",
                        legacy_state,
                        legacy.get("method"),
                        legacy.get("fuente_url"),
                        agent_state,
                        agent.get("method"),
                        agent.get("similarity"),
                        agent.get("fuente_url"),
                        is_critical,
                        diff_type,
                    )
        except Exception as e:
            logger.warning("_log_shadow_diffs failed: %s", e)

    async def run(self, req: GenerationRequest) -> AsyncIterator[bytes]:
        """Pipeline completo. Yield bytes SSE."""
        generation_id = str(uuid.uuid4())
        started_at = time.monotonic()
        document_id = str(uuid.uuid4())  # M1: documento virtual; M2 vinculará a matter_documents

        # M19.5: threadId único para agrupar todos los thoughts en un mensaje del asistente
        import uuid as _uuid_thread
        import time as _time_m19
        agent_thread_id = _uuid_thread.uuid4().hex[:16]

        # M19.5: buffer in-memory de TODOS los thoughts emitidos durante este run.
        # Helper para empujar al buffer Y emitir bytes SSE en una sola llamada.
        agent_thoughts_buffer: list[dict] = []

        def _push_thought(message: str, kind: str = "narration", **kwargs) -> bytes:
            """Empuja al buffer (para persist al final) + retorna bytes SSE."""
            payload = {"message": message, "kind": kind, **kwargs}
            agent_thoughts_buffer.append({**payload, "_ts": int(_time_m19.time() * 1000)})
            return SSEEvent.agent_thought(**payload)

        # ===== STAGE 0: PRE-FLIGHT CHECK (M18.d / M19.5) =====
        # Detecta errores legales obvios en el prompt antes de generar.
        # No bloquea — solo advierte via agent_thought.
        pre_findings_list = []
        try:
            import os as _os_pf
            preflight_enabled = _os_pf.getenv("PREFLIGHT_CHECK_ENABLED", "true").lower() in ("true", "1", "yes")
            if preflight_enabled and self.client is not None:
                from lex.verify.preflight_check import preflight_check

                # Tool call event para pre-flight (visible como chip colapsable)
                _pf_id = _uuid_thread.uuid4().hex[:12]
                yield _push_thought(
                    "Llamando preflight_check...",
                    kind="tool_call", tool="preflight_check", tool_id=_pf_id,
                    tool_request={"intent": req.intent[:500], "brief": (req.user_brief or "")[:300]},
                    thread_id=agent_thread_id,
                )
                pre_res = await preflight_check(self.client, req.intent, req.user_brief or "")
                pre_findings_list = [{"severity": f.severity, "issue": f.issue, "suggestion": f.suggestion} for f in pre_res.findings]
                yield _push_thought(
                    f"preflight_check completado en {pre_res.duration_ms}ms",
                    kind="tool_call", tool="preflight_check", tool_id=_pf_id,
                    tool_response={"findings": pre_findings_list, "overall_assessment": pre_res.overall_assessment},
                    tool_duration_ms=pre_res.duration_ms,
                    thread_id=agent_thread_id,
                )

                # Narración con LLM sobre los findings
                from lex.verify.narrator_agent import narrate
                narration = await narrate(self.client, "post_preflight", {"findings": pre_findings_list, "findings_json": pre_findings_list})
                if narration.text:
                    yield _push_thought(narration.text, kind="narration", thread_id=agent_thread_id)
        except Exception as e:
            logger.warning("preflight stage failed: %s", e)

        # ===== STAGE 1: CLASSIFIER =====
        yield sse("classification_started", {"intent_preview": req.intent[:200]})
        try:
            classification = await classify(self.client, req.intent, req.user_brief)
        except Exception as e:
            yield SSEEvent.error("classifier", str(e))
            return

        # Override si user pasó doc_type explícito
        if req.doc_type:
            classification.doc_type = req.doc_type

        plan = _resolve_plan(classification.doc_type)
        template = _resolve_template(classification.doc_type)

        yield SSEEvent.classification_done(
            doc_type=classification.doc_type,
            jurisdiccion=classification.jurisdiccion,
            materia=classification.materia,
            confidence=classification.confidence,
        )
        # M19.7: narrator post_classification (LLM)
        try:
            from lex.verify.narrator_agent import narrate
            pc = await narrate(self.client, "post_classification", {
                "doc_type": classification.doc_type,
                "jurisdiccion": classification.jurisdiccion,
                "materia": classification.materia,
                "confidence": round(classification.confidence, 2),
            })
            if pc.text:
                yield _push_thought(pc.text, kind="narration", thread_id=agent_thread_id)
        except Exception as e:
            logger.warning("narrator post_classification failed: %s", e)

        # Narración intro con LLM (estilo Claude)
        try:
            from lex.verify.narrator_agent import narrate
            intro = await narrate(self.client, "intro", {
                "intent_preview": req.intent[:400],
                "doc_type": classification.doc_type,
                "jurisdiccion": classification.jurisdiccion,
                "materia": classification.materia,
                "n_citations": "varias",
            })
            if intro.text:
                yield _push_thought(intro.text, kind="narration", thread_id=agent_thread_id)
        except Exception as e:
            logger.warning("narrator intro failed: %s", e)

        template_selected_meta = {
            "id": classification.doc_type,
            "name": template.nombre if template else classification.doc_type.replace("_", " ").title(),
            "jurisdiccion": template.jurisdiccion if template else classification.jurisdiccion,
            "description": template.description if template else "",
            "from_registry": template is not None,
        }

        yield SSEEvent.meta(
            generation_id=generation_id,
            template_selected=template_selected_meta,
            sections_plan=[
                {"key": s["key"], "title": s["title"], "order": s["order"], "roman": s.get("roman")}
                for s in plan
            ],
            estimated_seconds=max(15, len(plan) * 2),
        )

        # ===== STAGE 2: DATA EXTRACTOR =====
        yield sse("extraction_started", {})
        try:
            extraction = await extract(
                self.client, classification.doc_type, req.intent, req.user_brief
            )
        except Exception as e:
            yield SSEEvent.error("extractor", str(e))
            extraction = None

        yield SSEEvent.extraction_done(
            extracted_fields=(extraction.extracted_fields if extraction else {}),
            missing_fields=(extraction.missing_fields if extraction else []),
        )
        # M19.7: narrator post_extraction
        try:
            from lex.verify.narrator_agent import narrate
            fields_summary = {}
            if extraction and extraction.extracted_fields:
                # Solo top 8 fields para no saturar el prompt
                fields_summary = dict(list(extraction.extracted_fields.items())[:8])
            pe = await narrate(self.client, "post_extraction", {
                "extracted_summary": fields_summary or "(datos básicos identificados)",
            })
            if pe.text:
                yield _push_thought(pe.text, kind="narration", thread_id=agent_thread_id)
        except Exception as e:
            logger.warning("narrator post_extraction failed: %s", e)
        extracted_data = extraction.extracted_fields if extraction else {}

        # ===== STAGE 3: CALCULADORA (Python puro, cero LLM) =====
        calculations: dict[str, Any] = {}
        if template and template.calculadora:
            yield sse("calculation_started", {"calculadora": template.calculadora})
            try:
                calc_result = run_calculadora(template.calculadora, extracted_data)
                if calc_result:
                    calculations = calc_result
                    # Resumen para timeline
                    conceptos_summary = {}
                    if "conceptos" in calc_result:
                        for k, v in calc_result["conceptos"].items():
                            if isinstance(v, dict) and "valor" in v:
                                conceptos_summary[v.get("concepto", k)] = f"${v['valor']:,.0f}"
                    elif "valor" in calc_result:
                        conceptos_summary[calc_result.get("concepto", "valor")] = f"${calc_result['valor']:,.0f}"
                    yield SSEEvent.calculation_done(
                        conceptos=conceptos_summary,
                        total=calc_result.get("total"),
                    )
                else:
                    yield SSEEvent.calculation_done(conceptos={}, total=None)
            except Exception as e:
                logger.warning("calculadora stage failed: %s", e)
                yield SSEEvent.calculation_done(conceptos={}, total=None)

        # ===== STAGE 4: HUNTERS (RAG multi-query paralelo) =====
        jurisprudencia: list[dict] = []
        if template and template.hunters:
            yield sse("hunters_started", {"queries_count": len(template.hunters)})
            try:
                jurisprudencia = await run_template_hunters(template, self.client, self.pool)
                for hit in jurisprudencia[:10]:  # emitir solo top 10 al timeline
                    yield SSEEvent.jurisprudence_query(
                        query=hit.get("query_origen", ""),
                        hits=[{
                            "id": hit.get("id"),
                            "mp": hit.get("mp"),
                            "similarity": hit.get("similarity"),
                            "doc_title": hit.get("doc_title"),
                        }],
                        hunter=hit.get("corte", "general"),
                    )
                yield sse("hunters_done", {
                    "total_hits": len(jurisprudencia),
                    "sentencias": sum(1 for h in jurisprudencia if h.get("id")),
                })
                # M19.7: narrator post_hunters
                try:
                    from lex.verify.narrator_agent import narrate
                    ph = await narrate(self.client, "post_hunters", {
                        "hunters_summary": {
                            "total": len(jurisprudencia),
                            "ejemplos": [
                                (h.get("providencia") or h.get("doc_title") or "")[:80]
                                for h in jurisprudencia[:5]
                            ],
                        },
                    })
                    if ph.text:
                        yield _push_thought(ph.text, kind="narration", thread_id=agent_thread_id)
                except Exception as e:
                    logger.warning("narrator post_hunters failed: %s", e)
            except Exception as e:
                logger.warning("hunters stage failed: %s", e)
                yield sse("hunters_done", {"total_hits": 0, "error": str(e)[:120]})

        # ===== STAGE 6.5: M19.8 SECTION PLANNER (LLM declara citas por sección) =====
        # Feature flag USE_SECTION_PLAN_LOOP controla si activamos el plan-and-execute.
        # Si está deshabilitado o falla el LLM, seguimos con el flow tradicional.
        import os as _os_sp
        USE_SECTION_PLAN = _os_sp.getenv("USE_SECTION_PLAN_LOOP", "true").lower() in ("true", "1", "yes")
        section_plan_obj: dict[str, list[dict]] = {}  # {section_key: [{ref,type,relevance,purpose}]}
        section_verdicts: dict[str, list[dict]] = {}   # {section_key: [verdict dicts pre-verificados]}

        if USE_SECTION_PLAN and self.client is not None:
            try:
                from lex.orchestrator.stages.section_planner import plan_sections
                _plan_id = uuid.uuid4().hex[:12]
                yield _push_thought(
                    "Planificando qué citas legales requiere cada sección del documento...",
                    kind="tool_call", tool="section_planner", tool_id=_plan_id,
                    tool_request={"doc_type": classification.doc_type, "n_sections": len(plan)},
                    thread_id=agent_thread_id,
                )
                sp = await plan_sections(
                    client=self.client,
                    intent=req.intent,
                    brief=req.user_brief or "",
                    doc_type=classification.doc_type,
                    sections_plan=plan,
                    hunters_results=jurisprudencia or [],
                )
                if sp.by_section and not sp.error:
                    section_plan_obj = {
                        k: [c.to_dict() for c in v]
                        for k, v in sp.by_section.items()
                    }
                yield _push_thought(
                    f"Plan declarado: {sp.total_citations()} citas distribuidas en {len(sp.by_section)} secciones",
                    kind="tool_call", tool="section_planner", tool_id=_plan_id,
                    tool_response=sp.to_summary_dict(),
                    tool_duration_ms=sp.duration_ms,
                    thread_id=agent_thread_id,
                )

                # Narración del plan
                if sp.by_section:
                    plan_lines = []
                    for sec_key, cits in sp.by_section.items():
                        if not cits:
                            continue
                        sec_title = next((s["title"] for s in plan if s["key"] == sec_key), sec_key)
                        top_refs = [c.ref for c in cits[:3]]
                        plan_lines.append(f"- **{sec_title}**: {', '.join(top_refs)}" + (" …" if len(cits) > 3 else ""))
                    if plan_lines:
                        plan_msg = "**Plan de citas por sección:**\n\n" + "\n".join(plan_lines)
                        yield _push_thought(plan_msg, kind="narration", thread_id=agent_thread_id)

                # Pre-verificar TODAS las citas únicas declaradas
                if USE_AGENT_PREDECLARE := USE_SECTION_PLAN and section_plan_obj:
                    try:
                        from lex.verify.verification_agent import VerificationAgent
                        all_expected_cits: list[dict] = []
                        seen_refs: set[str] = set()
                        for sec_key, cits in section_plan_obj.items():
                            for c in cits:
                                k = (c["ref"], c["type"])
                                if k not in seen_refs:
                                    seen_refs.add(k)
                                    all_expected_cits.append({"ref": c["ref"], "type": c["type"]})
                        if all_expected_cits:
                            pre_thought_buffer: list[dict] = []
                            def _capture_pre(message: str, kind: str = "info", **extra):
                                extra.setdefault("thread_id", agent_thread_id)
                                pre_thought_buffer.append({"message": message, "kind": kind, **extra})
                            pre_agent = VerificationAgent(
                                self.client, self.pool,
                                firm_id=req.firm_id, user_id=None,
                                on_thought=_capture_pre,
                            )
                            pre_verdicts = await pre_agent.verify_batch(all_expected_cits)
                            # Flush narrations (tool calls)
                            for th in pre_thought_buffer:
                                yield _push_thought(**th)
                            # Indexar verdicts por ref
                            verdict_by_ref: dict[str, dict] = {}
                            for v in pre_verdicts:
                                verdict_by_ref[v.citation_text] = v.to_audit_dict()
                            # Distribuir verdicts a cada sección
                            for sec_key, cits in section_plan_obj.items():
                                section_verdicts[sec_key] = [
                                    verdict_by_ref[c["ref"]] for c in cits
                                    if c["ref"] in verdict_by_ref
                                ]
                    except Exception as e:
                        logger.warning("section_plan pre-verify failed: %s", e)
            except Exception as e:
                logger.warning("section_planner stage failed: %s", e)

        # ===== STAGE 7: BLOCK GENERATOR (por sección, streaming) =====
        all_blocks: list[dict[str, Any]] = []
        block_order = 0
        previous_sections_summary = ""
        citations_collected: list[dict] = []

        # Title block inicial
        title_b = TitleBlock(text=classification.doc_type.replace("_", " ").upper(), level=0)
        block_order += 1
        title_payload = {
            "section_key": "title",
            "block_order": block_order,
            "block_id": title_b.block_id,
            "block_type": title_b.type,
            "block_data": title_b.model_dump(),
        }
        all_blocks.append(title_payload)
        yield SSEEvent.block_emit("title", title_b.model_dump())
        yield SSEEvent.block_done(title_b.block_id)

        # Map de section_instruction y expected_blocks por section_key (si hay template)
        section_meta: dict[str, dict] = {}
        if template:
            for s in template.sections_plan:
                section_meta[s.key] = {
                    "instruction": s.section_instruction or "",
                    "expected_blocks": s.expected_blocks,
                }

        for section in plan:
            section_key = section["key"]
            section_order = section["order"]
            section_title = section["title"]
            roman = section.get("roman")
            extra_instr = section_meta.get(section_key, {}).get("instruction", "")
            expected_blocks = section_meta.get(section_key, {}).get("expected_blocks", [])

            yield SSEEvent.section_started(section_key, section_order, len(plan))

            # M19.7: narrator section_intro (LLM corto, anuncia qué va a redactar)
            try:
                from lex.verify.narrator_agent import narrate
                # M19.8: si tenemos section_plan con citas esperadas, las pasamos como contexto
                citations_for_section = ""
                if 'section_plan_obj' in locals() and section_plan_obj:
                    sec_plan = section_plan_obj.get(section_key)
                    if sec_plan and sec_plan.get("expected_citations"):
                        cits = sec_plan["expected_citations"][:4]
                        citations_for_section = "Citas a integrar: " + ", ".join(
                            [c.get("ref", "?") for c in cits]
                        )
                si = await narrate(self.client, "section_intro", {
                    "section_title": section_title,
                    "section_key": section_key,
                    "citations_summary": citations_for_section or "(sin citas específicas)",
                })
                if si.text:
                    yield _push_thought(si.text, kind="narration", thread_id=agent_thread_id)
            except Exception:
                pass

            # Emitir SectionHeadingBlock si tiene romano
            if roman:
                heading = SectionHeadingBlock(
                    roman=roman, text=section_title, section_key=section_key
                )
                block_order += 1
                hp = {
                    "section_key": section_key,
                    "block_order": block_order,
                    "block_id": heading.block_id,
                    "block_type": heading.type,
                    "block_data": heading.model_dump(),
                }
                all_blocks.append(hp)
                yield SSEEvent.block_emit(section_key, heading.model_dump())
                yield SSEEvent.block_done(heading.block_id)

            # Generar bloques de la sección
            section_content_acc: list[str] = []
            # M19.8: citas pre-verificadas para esta sección (si hay plan)
            section_verified_cits = section_verdicts.get(section_key, [])
            try:
                async for block in generate_section_blocks(
                    client=self.client,
                    doc_type=classification.doc_type,
                    section_key=section_key,
                    section_title=section_title,
                    section_order=section_order,
                    intent=req.intent,
                    brief=req.user_brief,
                    extracted_data=extracted_data,
                    previous_sections_summary=previous_sections_summary[-2000:],
                    calculations=calculations,
                    jurisprudencia=jurisprudencia,
                    section_instruction=extra_instr,
                    expected_blocks=expected_blocks,
                    verified_citations=section_verified_cits,  # M19.8
                ):
                    block_order += 1
                    payload = {
                        "section_key": section_key,
                        "block_order": block_order,
                        "block_id": block.block_id,
                        "block_type": block.type,
                        "block_data": block.model_dump(),
                    }
                    all_blocks.append(payload)
                    yield SSEEvent.block_emit(section_key, block.model_dump())
                    yield SSEEvent.block_done(block.block_id)

                    # Acumular texto para contexto
                    text_repr = _block_text_preview(block)
                    if text_repr:
                        section_content_acc.append(text_repr)

                    # Recolectar citas (M19.8: con section_key para summary)
                    if block.type == "norma_citada":
                        citations_collected.append({
                            "type": "norma",
                            "ref": block.norma,
                            "block_id": block.block_id,
                            "section_key": section_key,
                            "verified": block.verified,
                            "derogada": block.derogada,
                        })
                    elif block.type == "jurisprudencia":
                        citations_collected.append({
                            "type": "jurisprudencia",
                            "ref": block.id,
                            "mp": block.mp,
                            "corte": block.corte,
                            "block_id": block.block_id,
                            "section_key": section_key,
                            "verified": block.verified,
                        })

            except Exception as e:
                logger.exception("section %s failed: %s", section_key, e)
                yield SSEEvent.error(f"block_generator:{section_key}", str(e))

            yield SSEEvent.section_done(section_key)

            # M19.7: narrator section_summary (LLM corto, confirma sección lista)
            try:
                from lex.verify.narrator_agent import narrate
                n_blocks_section = sum(1 for b in all_blocks if b.get("section_key") == section_key)
                n_cit_used = sum(
                    1 for c in citations_collected
                    if c.get("section_key") == section_key
                )
                ss = await narrate(self.client, "section_summary", {
                    "section_title": section_title,
                    "n_blocks": n_blocks_section,
                    "n_citations_used": n_cit_used,
                })
                if ss.text:
                    yield _push_thought(ss.text, kind="narration", thread_id=agent_thread_id)
            except Exception:
                pass

            if section_content_acc:
                previous_sections_summary += f"\n[{section_title}]: " + " ".join(section_content_acc)[:600]

        # ===== STAGE: CITATION VERIFY + DEROGATION =====
        # M14: feature flag USE_VERIFICATION_AGENT controla cuál path se usa.
        # SHADOW_MODE corre ambos en paralelo y registra divergencias.
        import os as _os
        USE_AGENT = _os.getenv("USE_VERIFICATION_AGENT", "0").lower() in ("1", "true")
        SHADOW = _os.getenv("SHADOW_MODE", "0").lower() in ("1", "true")

        verification_results: list[dict] = []
        derogation_results: list[dict] = []
        if citations_collected and self.pool is not None:
            yield sse("citation_verify_started", {
                "total": len(citations_collected),
                "use_agent": USE_AGENT,
                "shadow_mode": SHADOW,
            })
            try:
                from lex.verify import CitationVerifier, DerogationVerifier
                derogation_verifier = DerogationVerifier(self.pool)

                # ── Path LEGACY (siempre se calcula si flag off o shadow on) ──
                legacy_results: list[dict] = []
                if not USE_AGENT or SHADOW:
                    citation_verifier = CitationVerifier(self.client, self.pool)
                    # M17: garantizar fuente_url incluso en path legacy
                    from utils.citation_url_builder import guarantee_fuente_url
                    from utils.citation_verifier import parse_citation_ref
                    for cit in citations_collected:
                        ref = cit.get("ref", "")
                        ctype = cit.get("type", "norma")
                        cv = await citation_verifier.verify(ref, ctype)
                        existing_url = getattr(cv, "fuente_url", None)
                        # Garantía M17: si verificada o derogada, debe haber URL
                        parsed = parse_citation_ref(ref)
                        guaranteed = guarantee_fuente_url(parsed, existing_url) if parsed else existing_url
                        legacy_results.append({
                            "ref": ref, "type": ctype,
                            "verified": cv.verified, "chunk_id": cv.chunk_id,
                            "similarity": cv.similarity,
                            "estado": getattr(cv, "estado", "verificada" if cv.verified else "no_encontrada"),
                            "method": cv.method,
                            "fuente_url": guaranteed,
                            "fuente_url_original": guaranteed,
                            "fuente_url_vigente": None,
                            "url_http_status": None,
                            "url_validated": False,
                            "is_derogada": getattr(cv, "estado", None) == "superada",
                            "titulo": getattr(cv, "titulo", None),
                        })

                # ── Path AGENT (M14) ──
                agent_results: list[dict] = []
                if USE_AGENT:
                    from lex.verify.verification_agent import VerificationAgent
                    # M18.d + M19.5: buffer thoughts del agente con threadId común
                    thought_buffer: list[dict] = []
                    def _capture_thought(message: str, kind: str = "info", **extra):
                        # Agregar thread_id si no viene
                        extra.setdefault("thread_id", agent_thread_id)
                        thought_buffer.append({"message": message, "kind": kind, **extra})

                    agent = VerificationAgent(
                        self.client, self.pool,
                        firm_id=req.firm_id, user_id=None,
                        on_thought=_capture_thought,
                    )
                    verdicts = await agent.verify_batch(citations_collected)
                    agent_results = [v.to_audit_dict() for v in verdicts]
                    # Flush buffered thoughts (tool calls)
                    for thought in thought_buffer:
                        yield _push_thought(**thought)

                    # Narración post-verification con LLM
                    n_ok = sum(1 for v in verdicts if v.verified)
                    n_corrections = sum(1 for v in verdicts if v.suggested_correction)
                    n_notes = sum(1 for v in verdicts if v.legal_note)
                    n_not_found = sum(1 for v in verdicts if v.estado == "no_encontrada")
                    corrections_list = [
                        {"original": v.citation_text, "sugerida": v.suggested_correction, "rationale": v.judge_rationale}
                        for v in verdicts if v.suggested_correction
                    ]
                    notes_list = [
                        {"cita": v.citation_text, "nota": v.legal_note}
                        for v in verdicts if v.legal_note
                    ]
                    try:
                        from lex.verify.narrator_agent import narrate
                        post_verif = await narrate(self.client, "post_verification", {
                            "total": len(verdicts),
                            "verified": n_ok,
                            "not_found": n_not_found,
                            "corrections": n_corrections,
                            "legal_notes": n_notes,
                            "corrections_list": corrections_list[:5],
                            "notes_list": notes_list[:5],
                        })
                        if post_verif.text:
                            yield _push_thought(post_verif.text, kind="narration", thread_id=agent_thread_id)
                    except Exception as e:
                        logger.warning("narrator post_verification failed: %s", e)

                # ── Decidir cuál ganara según flags ──
                if USE_AGENT and SHADOW:
                    # Ambos corrieron: legacy gana (seguridad), agent solo se loguea
                    verification_results = legacy_results
                    await self._log_shadow_diffs(legacy_results, agent_results, generation_id)
                elif USE_AGENT:
                    verification_results = agent_results
                else:
                    verification_results = legacy_results

                # Emitir eventos SSE individuales (compat con frontend)
                for v in verification_results:
                    yield SSEEvent.citation_verify(
                        citation=v.get("ref", ""),
                        found=v.get("verified", False),
                        chunk_id=v.get("chunk_id"),
                        similarity=v.get("similarity"),
                    )

                # Derogation (independiente, una sola vez)
                # M18: usar norma_url_index (cache compartido) + SmartSearchTool fallback
                from utils.citation_url_builder import build_url_candidates, build_search_fallback_url
                from utils.url_validator import find_valid_url
                from utils.citation_verifier import parse_citation_ref as _parse
                from utils.norma_url_index import lookup_norma_url

                async def _resolve_url_for_norma(parsed_obj):
                    """M18: cascade -> norma_url_index -> patterns -> Google fallback."""
                    if not parsed_obj:
                        return None
                    # 1. Cache compartido norma_url_index
                    indexed = await lookup_norma_url(parsed_obj, self.pool)
                    if indexed and indexed.fuente_url and indexed.url_validated:
                        return indexed.fuente_url
                    # 2. Patterns + HEAD cascade
                    cands = build_url_candidates(parsed_obj)
                    if cands:
                        valid, _ = await find_valid_url(cands, self.pool)
                        if valid:
                            return valid
                        return cands[0]
                    # 3. Último recurso: Google search honesto
                    return build_search_fallback_url(parsed_obj)

                for cit in citations_collected:
                    if cit.get("type") == "norma":
                        ref = cit.get("ref", "")
                        dc = await derogation_verifier.check(ref)
                        parsed_norma = _parse(ref)
                        fuente_url = await _resolve_url_for_norma(parsed_norma)
                        # Si derogada, URL vigente
                        fuente_url_vigente = None
                        if not dc.vigente and dc.derogada_por:
                            parsed_v = _parse(dc.derogada_por)
                            fuente_url_vigente = await _resolve_url_for_norma(parsed_v)
                        derogation_results.append({
                            "norma": ref, "vigente": dc.vigente,
                            "derogada_por": dc.derogada_por,
                            "fuente_url": fuente_url,
                            "fuente_url_vigente": fuente_url_vigente,
                        })
                        yield SSEEvent.derogation_check(
                            norma=ref, vigente=dc.vigente, derogada_por=dc.derogada_por,
                        )

                yield sse("citation_verify_done", {
                    "total": len(citations_collected),
                    "verified": sum(1 for v in verification_results if v.get("verified")),
                    "path": "agent" if (USE_AGENT and not SHADOW) else ("shadow" if SHADOW else "legacy"),
                })
            except Exception as e:
                logger.warning("citation/derogation stage failed: %s", e)

        # ===== STAGE 9: POLISH PASS (gpt-4o) =====
        polish_info: dict[str, Any] = {}
        try:
            from lex.orchestrator.stages.polish import run_polish
            yield SSEEvent.polish_started(model="gpt-4o", draft_chars=sum(
                len(str(b.get("block_data", {}))) for b in all_blocks
            ))
            # M19.7: narrator polish_intro
            try:
                from lex.verify.narrator_agent import narrate
                pi = await narrate(self.client, "polish_intro", {})
                if pi.text:
                    yield _push_thought(pi.text, kind="narration", thread_id=agent_thread_id)
            except Exception:
                pass

            polish_info = await run_polish(self.client, all_blocks, classification.doc_type)
            yield SSEEvent.polish_done(
                polished_chars=polish_info.get("delta_chars", 0),
                delta_chars=polish_info.get("delta_chars", 0),
                error=polish_info.get("error"),
            )
        except Exception as e:
            logger.warning("polish stage exception: %s", e)

        # ===== STAGE 9.5: M19.20.B — COMPLETENESS CHECK (post-polish, pre-QA) =====
        completeness_report = None
        try:
            from lex.orchestrator.stages.completeness_check import check_completeness
            completeness_report = await check_completeness(
                client=self.client,
                blocks=all_blocks,
                doc_type=classification.doc_type,
                run_llm_check=True,
            )
            yield SSEEvent.completeness_check_done(completeness_report.to_dict())
        except Exception as e:
            logger.warning("completeness_check stage exception: %s", e)

        # ===== STAGE 9.6: M19.20.E — AUTO-LOOP de corrección (opcional via flag) =====
        # Si completeness reporta gaps críticos y QUALITY_AUTOLOOP=true, intentamos
        # regenerar las secciones gap. Max 2 iteraciones.
        AUTOLOOP_ENABLED = os.getenv("QUALITY_AUTOLOOP", "false").lower() in ("1", "true", "yes")
        if (
            AUTOLOOP_ENABLED
            and completeness_report is not None
            and not completeness_report.can_continue
        ):
            try:
                MAX_AUTOLOOP_ITERATIONS = 2
                for iteration in range(1, MAX_AUTOLOOP_ITERATIONS + 1):
                    # Identificar secciones críticas faltantes
                    critical_sections: list[str] = list({
                        g.section_key for g in completeness_report.gaps
                        if g.severity == "critical" and g.section_key
                    })
                    if not critical_sections:
                        break
                    yield SSEEvent.autoloop_iteration(
                        iteration=iteration,
                        gaps_remaining=completeness_report.critical_count,
                        regenerating_sections=critical_sections,
                    )
                    logger.info(
                        "autoloop iter=%d: regenerando %d secciones críticas: %s",
                        iteration, len(critical_sections), critical_sections,
                    )
                    # Re-invocar block_generator solo para esas secciones
                    for sec_key in critical_sections:
                        # Encontrar el SectionPlanItem correspondiente si existe en el plan
                        sec_plan_item = next(
                            (p for p in plan if getattr(p, "key", None) == sec_key), None
                        )
                        if sec_plan_item is None:
                            continue
                        # Borrar bloques existentes de la sección (excepto heading)
                        all_blocks[:] = [
                            b for b in all_blocks
                            if (b.get("section_key") or b.get("block_data", {}).get("section_key")) != sec_key
                            or (b.get("block_type") or b.get("block_data", {}).get("type")) == "section_heading"
                        ]
                        # Re-generar bloques de la sección
                        try:
                            async for block in generate_section_blocks(
                                client=self.client,
                                doc_type=classification.doc_type,
                                section_key=sec_key,
                                section_title=getattr(sec_plan_item, "title", sec_key),
                                section_order=getattr(sec_plan_item, "order", 99),
                                intent=req.intent,
                                brief=req.user_brief,
                                extracted_data=extracted_data,
                                previous_sections_summary="",
                                calculations=calculations,
                                jurisprudencia=jurisprudencia,
                                section_instruction=f"REGENERACIÓN AUTO-CORRECCIÓN: la sección anterior fue marcada como incompleta. Genera AHORA todos los bloques sustantivos requeridos.",
                                expected_blocks=None,
                                verified_citations=section_verdicts.get(sec_key, []),
                            ):
                                block_order += 1
                                payload = {
                                    "section_key": sec_key,
                                    "block_order": block_order,
                                    "block_id": block.block_id,
                                    "block_type": block.type,
                                    "block_data": block.model_dump(),
                                }
                                all_blocks.append(payload)
                                yield SSEEvent.block_emit(sec_key, block.model_dump())
                                yield SSEEvent.block_done(block.block_id)
                        except Exception as e:
                            logger.warning("autoloop regen sec=%s failed: %s", sec_key, e)
                    # Re-correr completeness
                    completeness_report = await check_completeness(
                        client=self.client,
                        blocks=all_blocks,
                        doc_type=classification.doc_type,
                        run_llm_check=False,  # rule-only en iteraciones, más rápido
                    )
                    yield SSEEvent.completeness_check_done(completeness_report.to_dict())
                    if completeness_report.can_continue:
                        logger.info("autoloop converged after iteration %d", iteration)
                        break
            except Exception as e:
                logger.warning("autoloop exception (non-fatal): %s", e)

        # ===== STAGE 10: QA AGENT (rule-based + template validation_rules) =====
        qa_result: dict[str, Any] = {}
        try:
            from lex.orchestrator.stages.qa import run_qa
            qa_result = await run_qa(all_blocks, template)
            yield SSEEvent.qa_done(
                passed=qa_result.get("passed", True),
                score=qa_result.get("score", 7.5),
                issues=qa_result.get("issues", []),
            )
            # M19.7: narrator qa_summary
            try:
                from lex.verify.narrator_agent import narrate
                qs = await narrate(self.client, "qa_summary", {
                    "score": round(qa_result.get("score", 0.0), 2),
                    "passed": qa_result.get("passed", True),
                    "n_issues": len(qa_result.get("issues", []) or []),
                    "issues_list": (qa_result.get("issues") or [])[:5],
                })
                if qs.text:
                    yield _push_thought(qs.text, kind="narration", thread_id=agent_thread_id)
            except Exception:
                pass
        except Exception as e:
            logger.warning("qa stage exception: %s", e)

        # M19.10.A7: Inyectar verdicts (fuente_url, derogada, etc.) en blocks
        # antes de persistir → el DOCX builder generará hyperlinks reales.
        try:
            _inject_verdicts_into_blocks(all_blocks, verification_results, derogation_results)
        except Exception as e:
            logger.debug("inject verdicts into blocks failed (non-fatal): %s", e)

        # M19.15.A.1 + M19.18.D — Dedup de bloques firma:
        #   1) Si hay múltiples `firma` blocks, conservar solo el último (sección "firma")
        #   2) Borrar paragraphs tipo firma (Atentamente, _____, T.P. del C.S.J.,
        #      Abogado · T.P., Email:, Tel.:) que estén FUERA de section_key="firma"
        try:
            firma_indices = [
                i for i, b in enumerate(all_blocks)
                if (b.get("block_type") or b.get("block_data", {}).get("type")) == "firma"
            ]
            drop_set: set[int] = set()
            if len(firma_indices) > 1:
                drop_set.update(firma_indices[:-1])
                logger.info(
                    "dedup firma blocks: kept index %d, dropped %d duplicates",
                    firma_indices[-1], len(firma_indices) - 1,
                )

            # M19.18.D — drop paragraphs con firma-like content fuera de section_key="firma"
            import re as _re
            FIRMA_RX = _re.compile(
                r"(?i)(atentamente,?$|^del\s+se[ñn]or\s+juez|_{15,}|"
                r"\babogad[oa]\s*[·\-]?\s*t\.?\s*p\.?\s*no\b|"
                r"\bemail:\s*\S+@|\btel\.?:\s*\d{6,})"
            )
            for i, b in enumerate(all_blocks):
                if i in drop_set:
                    continue
                bd = b.get("block_data") or {}
                bt = b.get("block_type") or bd.get("type")
                sk = b.get("section_key") or bd.get("section_key") or ""
                if bt != "paragraph" or sk == "firma":
                    continue
                runs = bd.get("runs") or []
                flat = " ".join(
                    (r.get("text", "") if isinstance(r, dict) else str(r))
                    for r in runs
                ).strip()
                if not flat:
                    continue
                if FIRMA_RX.search(flat):
                    drop_set.add(i)
                    logger.info(
                        "drop firma-like paragraph in section=%s: %r",
                        sk[:20], flat[:60],
                    )

            if drop_set:
                all_blocks[:] = [b for i, b in enumerate(all_blocks) if i not in drop_set]
        except Exception as e:
            logger.debug("firma dedup failed (non-fatal): %s", e)

        # ===== Persistir bloques en BD (best-effort) =====
        if self.blocks_repo:
            try:
                count = await self.blocks_repo.insert_blocks_batch(
                    document_id=document_id,
                    generation_id=generation_id,
                    blocks=all_blocks,
                )
                logger.info("persisted %d blocks for doc %s", count, document_id)
            except Exception as e:
                logger.warning("persist blocks failed: %s", e)

        # ===== Audit (M1: básico, M4 enriquece) =====
        duration_seconds = round(time.monotonic() - started_at, 2)
        # Estimación de costo M1 (placeholder)
        # gpt-4o-mini ~$0.15/1M input + $0.60/1M output (asumimos 5K tokens promedio)
        # gpt-4o ~$2.50/1M input + $10/1M output
        estimated_cost = round(0.005 + 0.003 * len(plan), 4)

        # Build consolidated audit report (M4 + M5 polish/qa)
        from lex.verify import build_audit_report
        audit_payload = build_audit_report(
            generation_id=generation_id,
            template_id=classification.doc_type,
            duration_seconds=duration_seconds,
            cost_usd=estimated_cost,
            classification={
                "doc_type": classification.doc_type,
                "jurisdiccion": classification.jurisdiccion,
                "materia": classification.materia,
                "confidence": classification.confidence,
            },
            extraction={
                "extracted_fields_count": len(extracted_data),
                "missing_fields": extraction.missing_fields if extraction else [],
            },
            calculations=calculations,
            citations=citations_collected,
            citation_verifications=verification_results,
            derogation_checks=derogation_results,
            qa_result=qa_result,
            polish_info=polish_info,
            total_blocks=len(all_blocks),
        )

        if self.audit_repo:
            try:
                await self.audit_repo.insert_audit(
                    generation_id=generation_id,
                    template_id=classification.doc_type,
                    firm_id=req.firm_id,
                    matter_id=req.matter_id,
                    document_id=document_id,
                    model_used={"classifier": "gpt-4o-mini", "extractor": "gpt-4o-mini",
                                "generator": "gpt-4o"},
                    duration_seconds=duration_seconds,
                    cost_usd=estimated_cost,
                    citations=citations_collected,
                    calculations=calculations,
                    validation_passed=False,  # M4 lo seteará
                    audit_json=audit_payload,
                )
            except Exception as e:
                logger.warning("audit insert failed: %s", e)

        yield SSEEvent.audit_report(audit_payload)

        # ===== STAGE 11.5: M19.20.C — COHERENCE CHECK (cross-section LLM) =====
        coherence_report = None
        try:
            from lex.orchestrator.stages.coherence_check import check_coherence
            coherence_report = await check_coherence(
                client=self.client,
                blocks=all_blocks,
                doc_type=classification.doc_type,
            )
            yield SSEEvent.coherence_check_done(coherence_report.to_dict())
        except Exception as e:
            logger.warning("coherence_check stage exception: %s", e)

        # ===== STAGE 11.6: M19.20.D — QUALITY REPORT UNIFICADO =====
        try:
            from lex.verify.quality_report import build_quality_report
            citation_rate = float(audit_payload.get("citation_existence_rate", 0.0) or 0.0)
            quality_report = build_quality_report(
                doc_type=classification.doc_type,
                completeness=completeness_report,
                coherence=coherence_report,
                qa_result=qa_result,
                citation_existence_rate=citation_rate,
            )
            yield SSEEvent.quality_report(quality_report.to_dict())
        except Exception as e:
            logger.warning("quality_report stage exception: %s", e)

        # M19.7: presented_file event con metadata DOCX (URL al export endpoint)
        try:
            docx_filename = f"{classification.doc_type}_{document_id[:8]}.docx"
            # URL relativa al export endpoint (frontend resuelve absoluto)
            docx_url = f"/api/documents/v2/documents/{document_id}/export-forensic"
            # M19.7 future: generar preview PNG con docx2pdf + pdf2image
            # Por ahora sin thumbnail (PresentedFileChip muestra icono DOCX genérico)
            yield SSEEvent.presented_file(
                name=docx_filename,
                url=docx_url,
                size_kb=None,  # se calcula on-demand
                preview_b64=None,
                thread_id=agent_thread_id,
            )
        except Exception as e:
            logger.debug("presented_file emit failed (non-fatal): %s", e)

        # M19.7: narrator synthesis (estilo Claude "5 correcciones importantes")
        try:
            from lex.verify.narrator_agent import narrate
            n_verified = sum(1 for v in verification_results if v.get("verified"))
            n_corrections = sum(1 for v in verification_results if v.get("suggested_correction"))
            corrections_applied = [
                {"original": v.get("ref"), "sugerida": v.get("suggested_correction")}
                for v in verification_results if v.get("suggested_correction")
            ][:8]
            syn = await narrate(self.client, "synthesis", {
                "result_summary": {
                    "doc_type": classification.doc_type,
                    "total_blocks": len(all_blocks),
                    "verified": n_verified,
                    "total_citations": len(verification_results),
                    "corrections": n_corrections,
                    "duration_seconds": duration_seconds,
                },
                "corrections_applied": corrections_applied or "(ninguna)",
            })
            if syn.text:
                yield _push_thought(syn.text, kind="narration", thread_id=agent_thread_id)
        except Exception as e:
            logger.warning("narrator synthesis failed: %s", e)

        # M19.5: persistir thread del agente en chat_messages
        if self.pool is not None:
            try:
                from lex.orchestrator.persistence_chat import persist_chat_thread
                await persist_chat_thread(
                    pool=self.pool,
                    thread_id=agent_thread_id,
                    generation_id=generation_id,
                    document_id=document_id,
                    firm_id=req.firm_id,
                    matter_id=req.matter_id,
                    user_intent=req.intent,
                    user_brief=req.user_brief or "",
                    agent_thoughts=agent_thoughts_buffer,
                    duration_ms=int(duration_seconds * 1000),
                )
            except Exception as e:
                logger.warning("chat_messages persist failed: %s", e)

        yield SSEEvent.done(
            generation_id=generation_id,
            matter_document_id=document_id,
            duration_seconds=duration_seconds,
            cost_usd=estimated_cost,
            total_blocks=len(all_blocks),
        )


def _block_text_preview(block: Block) -> str:
    """Extrae texto plano de un bloque para construir resumen de contexto."""
    t = block.type
    if t == "paragraph":
        return "".join(r.text for r in block.runs)[:200]
    if t == "hecho":
        return f"{block.num}. " + "".join(r.text for r in block.runs)[:200]
    if t == "pretension":
        return f"{block.ord}. " + "".join(r.text for r in block.runs)[:200]
    if t == "norma_citada":
        return f"[{block.norma}]"
    if t == "jurisprudencia":
        return f"[{block.id} M.P. {block.mp}]"
    if t == "subsection":
        return block.text
    if t == "calc_step":
        return f"{block.label}: {block.total}"
    return ""


def _inject_verdicts_into_blocks(
    all_blocks: list[dict[str, Any]],
    verification_results: list[dict],
    derogation_results: list[dict],
) -> None:
    """M19.10.A7: enriquece blocks norma_citada/jurisprudencia con datos del verdict.

    Esto permite que el DOCX builder genere hyperlinks reales a las fuentes
    oficiales gov.co usando `fuente_url` propagado del VerificationAgent.

    Mutación in-place del array all_blocks.
    """
    # Indexar verdicts por ref (case-insensitive simple)
    verdict_by_ref: dict[str, dict] = {}
    for v in verification_results or []:
        ref = (v.get("ref") or "").strip()
        if ref:
            verdict_by_ref[ref.lower()] = v
            verdict_by_ref[ref] = v  # preservar case original también

    derog_by_norma: dict[str, dict] = {}
    for d in derogation_results or []:
        norma = (d.get("norma") or "").strip()
        if norma:
            derog_by_norma[norma.lower()] = d
            derog_by_norma[norma] = d

    for b in all_blocks:
        bt = b.get("block_type") or b.get("block_data", {}).get("type")
        bd = b.get("block_data") or {}
        if bt not in ("norma_citada", "jurisprudencia"):
            continue

        # Resolver verdict matching por ref/norma/id
        if bt == "norma_citada":
            ref = bd.get("norma", "")
        else:  # jurisprudencia
            ref = bd.get("id", "")

        if not ref:
            continue

        # Match exact + lowercase fallback
        v = verdict_by_ref.get(ref) or verdict_by_ref.get(ref.lower())
        d = derog_by_norma.get(ref) or derog_by_norma.get(ref.lower())

        if v:
            # Propagar URLs verificadas al block_data (in-place)
            if v.get("fuente_url") and not bd.get("fuente_url"):
                bd["fuente_url"] = v["fuente_url"]
            if v.get("fuente_url_original") and not bd.get("fuente_url"):
                bd["fuente_url"] = v["fuente_url_original"]
            if v.get("fuente_url_vigente") and not bd.get("fuente_url_vigente"):
                bd["fuente_url_vigente"] = v["fuente_url_vigente"]
            if v.get("discovered_by") and not bd.get("discovered_by"):
                bd["discovered_by"] = v["discovered_by"]
            if v.get("verified") is not None:
                bd["verified"] = v["verified"]
            if v.get("is_derogada") and not bd.get("derogada"):
                bd["derogada"] = True
            if v.get("titulo") and not bd.get("titulo_oficial"):
                bd["titulo_oficial"] = v["titulo"]

        if d and bt == "norma_citada":
            # Si está derogada y tenemos URL vigente
            if not d.get("vigente"):
                bd["derogada"] = True
            if d.get("fuente_url_vigente") and not bd.get("fuente_url_vigente"):
                bd["fuente_url_vigente"] = d["fuente_url_vigente"]


async def run_pipeline(client, pool, req: GenerationRequest) -> AsyncIterator[bytes]:
    """Helper top-level para crear orchestrator y correr."""
    orch = Orchestrator(client, pool)
    async for chunk in orch.run(req):
        yield chunk
