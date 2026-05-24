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

    async def run(self, req: GenerationRequest) -> AsyncIterator[bytes]:
        """Pipeline completo. Yield bytes SSE."""
        generation_id = str(uuid.uuid4())
        started_at = time.monotonic()
        document_id = str(uuid.uuid4())  # M1: documento virtual; M2 vinculará a matter_documents

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
            except Exception as e:
                logger.warning("hunters stage failed: %s", e)
                yield sse("hunters_done", {"total_hits": 0, "error": str(e)[:120]})

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

                    # Recolectar citas
                    if block.type == "norma_citada":
                        citations_collected.append({
                            "type": "norma",
                            "ref": block.norma,
                            "block_id": block.block_id,
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
                            "verified": block.verified,
                        })

            except Exception as e:
                logger.exception("section %s failed: %s", section_key, e)
                yield SSEEvent.error(f"block_generator:{section_key}", str(e))

            yield SSEEvent.section_done(section_key)

            if section_content_acc:
                previous_sections_summary += f"\n[{section_title}]: " + " ".join(section_content_acc)[:600]

        # ===== STAGE: CITATION VERIFY + DEROGATION =====
        verification_results: list[dict] = []
        derogation_results: list[dict] = []
        if citations_collected and self.pool is not None:
            yield sse("citation_verify_started", {"total": len(citations_collected)})
            try:
                from lex.verify import CitationVerifier, DerogationVerifier
                citation_verifier = CitationVerifier(self.client, self.pool)
                derogation_verifier = DerogationVerifier(self.pool)
                for cit in citations_collected:
                    ref = cit.get("ref", "")
                    ctype = cit.get("type", "norma")
                    cv = await citation_verifier.verify(ref, ctype)
                    verification_results.append({
                        "ref": ref, "type": ctype,
                        "verified": cv.verified, "chunk_id": cv.chunk_id,
                        "similarity": cv.similarity,
                        "estado": getattr(cv, "estado", "verificada" if cv.verified else "no_encontrada"),
                        "method": cv.method,
                        "fuente_url": getattr(cv, "fuente_url", None),
                        "titulo": getattr(cv, "titulo", None),
                    })
                    yield SSEEvent.citation_verify(
                        citation=ref, found=cv.verified,
                        chunk_id=cv.chunk_id, similarity=cv.similarity,
                    )
                    if ctype == "norma":
                        dc = await derogation_verifier.check(ref)
                        derogation_results.append({
                            "norma": ref, "vigente": dc.vigente,
                            "derogada_por": dc.derogada_por,
                        })
                        yield SSEEvent.derogation_check(
                            norma=ref, vigente=dc.vigente, derogada_por=dc.derogada_por,
                        )
                yield sse("citation_verify_done", {
                    "total": len(citations_collected),
                    "verified": sum(1 for v in verification_results if v.get("verified")),
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
            polish_info = await run_polish(self.client, all_blocks, classification.doc_type)
            yield SSEEvent.polish_done(
                polished_chars=polish_info.get("delta_chars", 0),
                delta_chars=polish_info.get("delta_chars", 0),
                error=polish_info.get("error"),
            )
        except Exception as e:
            logger.warning("polish stage exception: %s", e)

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
        except Exception as e:
            logger.warning("qa stage exception: %s", e)

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


async def run_pipeline(client, pool, req: GenerationRequest) -> AsyncIterator[bytes]:
    """Helper top-level para crear orchestrator y correr."""
    orch = Orchestrator(client, pool)
    async for chunk in orch.run(req):
        yield chunk
