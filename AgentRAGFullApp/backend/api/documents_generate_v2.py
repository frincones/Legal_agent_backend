"""Sprint M · Router /v1/documents/v2 · Block-level streaming SSE.

Endpoint nuevo que NO afecta a /v1/documents/generate (v1).

Detrás de feature flag FLAG_DOCGEN_V2 (default false en prod):
- Si FLAG_DOCGEN_V2=true → endpoint operativo
- Si FLAG_DOCGEN_V2=false → devuelve 503 service unavailable

Eventos SSE emitidos:
  classification_started/done
  meta (con sections_plan)
  extraction_started/done
  (M3+) calculation_started/done, jurisprudence_query, hunters_done
  (M4+) derogation_check, citation_verify
  section_started/done
  block_emit, block_done
  (M5+) polish_started/done, qa_done, docx_built
  audit_report
  done
  error (recoverable)
"""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from lex.orchestrator import GenerationRequest, run_pipeline
from utils.db import get_storage
from utils.llm import get_openai_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/documents/v2", tags=["documents-v2"])


def _flag_enabled() -> bool:
    return os.getenv("FLAG_DOCGEN_V2", "false").lower() in ("1", "true", "yes")


class GenerateRequestBody(BaseModel):
    intent: str = Field(..., min_length=5, max_length=4000)
    user_brief: str = Field(default="", max_length=12000)
    matter_id: str | None = None
    firm_id: str | None = None
    materia: str | None = None
    doc_type: str | None = None
    context: dict[str, Any] | None = None
    # M19.23.C — modo del data_completeness_gate
    # True (default, backward compat): modo borrador, continúa con placeholders
    # False: modo firma, agente pausa si faltan datos críticos
    borrador_mode: bool = True


async def _require_session(request: Request) -> dict[str, Any]:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing_bearer_token")
    return {"token": auth[7:]}


class LegalClassifyRequestBody(BaseModel):
    """M19.24.B — Request body para /legal-classify (pre-research conceptual).

    Lo usa el BriefModal y el LegalReasoningCard del frontend para mostrar
    el análisis legal previo (régimen, naturaleza, correcciones de premisas,
    advertencias de riesgo) ANTES de generar el documento.
    """
    intent: str = Field(..., min_length=5, max_length=4000)
    doc_type: str | None = None


@router.post("/legal-classify")
async def legal_classify(
    body: LegalClassifyRequestBody,
    _claims: dict = Depends(_require_session),
):
    """M19.24.B — Clasifica conceptualmente el caso legal.

    Reproduce el Paso 3 de Claude (clasificación + corrección de premisas).
    Devuelve régimen aplicable, naturaleza del acto, fundamento normativo,
    premisas corregidas (e.g. Art. 836 CGP no existe) y advertencias de riesgo.

    Latencia esperada: 3-15s (cache HIT instantáneo).
    """
    if not _flag_enabled():
        raise HTTPException(status_code=503, detail="docgen_v2_disabled")

    client = get_openai_client()
    if client is None:
        raise HTTPException(status_code=503, detail="llm_client_unavailable")

    storage = await get_storage()
    from lex.orchestrator.stages.legal_classifier import classify_legal_case

    classification = await classify_legal_case(
        client=client,
        pool=storage.pool,
        intent=body.intent,
        doc_type_hint=body.doc_type,
        timeout_seconds=30.0,
    )
    return classification.to_dict()


class PreviewFieldsRequestBody(BaseModel):
    """M19.23.K — Request body para /preview-required-fields.

    Lo usa el BriefModal del frontend para mostrar dinámicamente los campos
    que el agente considera necesarios para redactar el documento.
    """
    intent: str = Field(..., min_length=5, max_length=4000)
    doc_type: str | None = None
    materia: str | None = None


@router.post("/preview-required-fields")
async def preview_required_fields(
    body: PreviewFieldsRequestBody,
    _claims: dict = Depends(_require_session),
):
    """M19.23.K — Predice campos requeridos ANTES de generar.

    Permite al BriefModal mostrar dinámicamente las preguntas que el agente
    consideraría necesarias para redactar el documento, en lugar de tener
    una lista hardcoded por template_id.

    Reusa 100% el LLM call de `data_completeness_gate.check_data_completeness`
    pasando solo intent + doc_type (sin extracted_data, sin sections_plan
    porque structure_discovery aún no ha corrido). Aún así, gpt-4o produce
    una lista exhaustiva razonando desde la norma procesal.

    Latencia esperada: 5-12s. El frontend muestra spinner.
    """
    if not _flag_enabled():
        raise HTTPException(status_code=503, detail="docgen_v2_disabled")

    client = get_openai_client()
    if client is None:
        raise HTTPException(status_code=503, detail="llm_client_unavailable")

    # Si el frontend no mandó doc_type, intentamos inferir uno genérico
    # (el preview funciona mejor si el doc_type ya viene del intent detector).
    doc_type = body.doc_type or "documento_legal_generico"

    from lex.orchestrator.stages.data_completeness_gate import check_data_completeness

    report = await check_data_completeness(
        client=client,
        doc_type=doc_type,
        intent=body.intent,
        brief=None,
        extracted_data=None,           # nada extraído todavía
        norma_procesal_ref=None,        # no hay structure_recipe aún
        juez_competente=None,
        sections_plan=None,
        borrador_mode=True,             # solo informativo, no bloquea
        timeout_seconds=20.0,
        model="gpt-4o",
    )

    return {
        "doc_type": report.doc_type,
        "fields_critical": [
            {
                "field_key": f.field_key,
                "label": f.label,
                "description": f.description,
                "example_value": f.example_value,
                "suggested_placeholder": f.suggested_placeholder,
            }
            for f in report.missing_critical
        ],
        "fields_optional": [
            {
                "field_key": f.field_key,
                "label": f.label,
                "description": f.description,
                "example_value": f.example_value,
                "suggested_placeholder": f.suggested_placeholder,
            }
            for f in report.missing_optional
        ],
        "required_fields_count": report.required_fields_count,
        "reasoning": report.reasoning,
        "skipped": report.skipped,
        "duration_ms": report.duration_ms,
    }


@router.post("/generate")
async def generate_v2(
    body: GenerateRequestBody,
    _claims: dict = Depends(_require_session),
):
    """Generación de documento v2 con block-level streaming."""
    if not _flag_enabled():
        raise HTTPException(
            status_code=503,
            detail="docgen_v2_disabled (set FLAG_DOCGEN_V2=true to enable)",
        )

    storage = await get_storage()
    client = get_openai_client()

    req = GenerationRequest(
        intent=body.intent,
        user_brief=body.user_brief or "",
        matter_id=body.matter_id,
        firm_id=body.firm_id,
        materia=body.materia,
        doc_type=body.doc_type,
        context=body.context or {},
        borrador_mode=body.borrador_mode,
    )

    return StreamingResponse(
        run_pipeline(client, storage.pool, req),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
        },
    )


@router.get("/documents/{document_id}/blocks")
async def get_document_blocks(
    document_id: str,
    _claims: dict = Depends(_require_session),
):
    """Recupera todos los bloques de un documento generado (refresh canvas)."""
    if not _flag_enabled():
        raise HTTPException(status_code=503, detail="docgen_v2_disabled")
    storage = await get_storage()
    from lex.storage import BlocksRepo
    repo = BlocksRepo(storage.pool)
    blocks = await repo.get_blocks_for_document(document_id)
    return {"document_id": document_id, "blocks": blocks, "total": len(blocks)}


@router.get("/documents/{document_id}/audit")
async def get_document_audit(
    document_id: str,
    _claims: dict = Depends(_require_session),
):
    """Recupera audit por document_id (busca el último generation_audit asociado)."""
    if not _flag_enabled():
        raise HTTPException(status_code=503, detail="docgen_v2_disabled")
    storage = await get_storage()
    try:
        async with storage.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM generation_audit
                WHERE document_id = $1
                ORDER BY created_at DESC
                LIMIT 1
            """, document_id)
        if not row:
            return JSONResponse({"document_id": document_id, "audit": None}, status_code=404)
    except Exception as e:
        logger.warning("audit fetch failed: %s", e)
        return JSONResponse({"document_id": document_id, "audit": None, "error": str(e)[:120]}, status_code=500)

    import json
    return {
        "document_id": document_id,
        "audit": {
            "generation_id": str(row["generation_id"]),
            "template_id": row["template_id"],
            "duration_seconds": float(row["duration_seconds"]) if row["duration_seconds"] else 0,
            "cost_usd": float(row["cost_usd"]) if row["cost_usd"] else 0,
            "citations": row["citations"] if isinstance(row["citations"], list) else json.loads(row["citations"] or "[]"),
            "validation_passed": row["validation_passed"],
            "audit_json": row["audit_json"] if isinstance(row["audit_json"], dict) else json.loads(row["audit_json"] or "{}"),
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        },
    }


@router.post("/documents/{document_id}/export-forensic")
@router.get("/documents/{document_id}/export-forensic")
async def export_forensic_docx(
    document_id: str,
    request: Request,
    _claims: dict = Depends(_require_session),
):
    """Exporta documento como .docx forense (Times NR, márgenes 3cm, justified)
    construyendo imperativamente desde bloques persistidos.

    M19.12.C1: persiste el DOCX en Supabase Storage bucket 'documents' la primera
    vez que se solicita, y en llamadas posteriores lo sirve desde cache (más rápido).

    M19.25.E · CAMINO B opcional · query param ``engine=claude``:
        Si llega ``?engine=claude`` AND CLAUDE_RENDERER_ENABLED=true AND el doc_type
        del documento está habilitado en CLAUDE_RENDERER_DOC_FAMILIES → usa
        lex.renderer.claude_docx_renderer.render_docx() (Claude Opus + docx-js).
        Si falla por cualquier motivo (timeout, JS error, anthropic API down) →
        fallback transparente al builder legacy. Auditado en claude_render_audit.
    """
    if not _flag_enabled():
        raise HTTPException(status_code=503, detail="docgen_v2_disabled")
    storage = await get_storage()
    from lex.storage import BlocksRepo
    from lex.docx_forensic_builder import build_docx_from_blocks
    from lex.storage.docx_storage import get_or_build_and_cache_docx
    from fastapi.responses import StreamingResponse
    import io as _io

    repo = BlocksRepo(storage.pool)
    blocks = await repo.get_blocks_for_document(document_id)
    if not blocks:
        raise HTTPException(status_code=404, detail="document_not_found_or_empty")

    # M19.25.E · Try Claude renderer (camino B) si activado por query param
    engine = (request.query_params.get("engine") or "").strip().lower()
    if engine == "claude":
        try:
            from lex.renderer import claude_docx_renderer as _crd
            doc_meta = await _resolve_document_meta_for_render(storage.pool, document_id)
            if _crd.is_renderer_enabled_for(
                doc_type=doc_meta.get("doc_type"),
                doc_family=doc_meta.get("doc_family"),
                firm_id=doc_meta.get("firm_id"),
            ):
                docx_bytes = await _try_render_with_claude(
                    pool=storage.pool,
                    document_id=document_id,
                    blocks=blocks,
                    doc_meta=doc_meta,
                )
                if docx_bytes:
                    return StreamingResponse(
                        _io.BytesIO(docx_bytes),
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        headers={
                            "Content-Disposition": f'attachment; filename="documento_{document_id[:8]}.docx"',
                            "X-LexAI-Renderer": "claude-opus",
                        },
                    )
                # docx_bytes None → falló y fallback a legacy
                logger.info("claude renderer returned None, falling back to legacy for %s", document_id)
            else:
                logger.info("claude renderer disabled for doc_meta=%s, using legacy", doc_meta)
        except Exception as e:
            # NUNCA romper el endpoint si Claude renderer falla
            logger.warning("claude renderer crashed (%s), falling back to legacy", e)

    try:
        # M19.12.C1: cache en Supabase Storage (fallback a build on-demand si falla)
        docx_bytes = await get_or_build_and_cache_docx(
            pool=storage.pool,
            document_id=document_id,
            builder=lambda: build_docx_from_blocks(
                blocks, title=f"Documento {document_id[:8]}", author="LexAI"
            ),
        )
    except Exception as e:
        logger.exception("docx forensic export failed")
        raise HTTPException(status_code=500, detail=f"export_error:{str(e)[:120]}")

    return StreamingResponse(
        _io.BytesIO(docx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            "Content-Disposition": f'attachment; filename="documento_{document_id[:8]}.docx"',
            "X-LexAI-Renderer": "legacy-forensic",
        },
    )


# ----------------------------------------------------------------
# M19.25.E · helpers para integración Claude renderer
# ----------------------------------------------------------------

async def _resolve_document_meta_for_render(pool, document_id: str) -> dict[str, Any]:
    """Best-effort: extrae doc_type, doc_family, firm_id, user_id, matter_id
    desde tablas conocidas (matter_documents, document_blocks metadata).

    Returns dict (puede tener None en cualquier campo).
    """
    meta: dict[str, Any] = {}
    try:
        async with pool.acquire() as conn:
            # matter_documents may not exist in all envs; try gracefully
            row = await conn.fetchrow(
                """
                select md.doc_type, md.doc_family, md.firm_id, md.user_id, md.matter_id
                  from matter_documents md
                 where md.id = $1::uuid
                """,
                document_id,
            )
            if row:
                meta.update({
                    "doc_type": row["doc_type"],
                    "doc_family": row["doc_family"],
                    "firm_id": str(row["firm_id"]) if row["firm_id"] else None,
                    "user_id": str(row["user_id"]) if row["user_id"] else None,
                    "matter_id": str(row["matter_id"]) if row["matter_id"] else None,
                })
                return meta
    except Exception as e:
        logger.debug("matter_documents lookup failed (likely table absent): %s", e)

    # Fallback: inferir desde document_blocks (primera fila tiene generation_id)
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                select dg.doc_type, dg.doc_family, dg.firm_id, dg.user_id, dg.matter_id
                  from document_blocks db
                  join document_generations dg on dg.id = db.generation_id
                 where db.document_id = $1::uuid
                 limit 1
                """,
                document_id,
            )
            if row:
                meta.update({
                    "doc_type": row["doc_type"],
                    "doc_family": row["doc_family"],
                    "firm_id": str(row["firm_id"]) if row["firm_id"] else None,
                    "user_id": str(row["user_id"]) if row["user_id"] else None,
                    "matter_id": str(row["matter_id"]) if row["matter_id"] else None,
                })
    except Exception as e:
        logger.debug("document_generations lookup failed: %s", e)
    return meta


async def _try_render_with_claude(
    *,
    pool,
    document_id: str,
    blocks: list[dict[str, Any]],
    doc_meta: dict[str, Any],
) -> bytes | None:
    """Intenta renderizar con Claude. None si no se puede (fallback) o si falla."""
    from lex.renderer.claude_docx_renderer import RenderRequest, render_docx, log_audit

    doc_type = doc_meta.get("doc_type")
    firm_id = doc_meta.get("firm_id")
    if not doc_type:
        return None

    # Resolver SKILL.md del template desde firm_skills (cascade firm_id → builtin)
    template_md = await _load_template_skill_md(pool, doc_type=doc_type, firm_id=firm_id)
    if not template_md:
        logger.info("no SKILL.md template found for doc_type=%s, falling back", doc_type)
        return None

    # Reconstruir el "user_prompt" desde blocks + extraer datos básicos
    user_prompt = "Genera el documento .docx final conforme al template. Usa el contenido " \
                  "existente de los bloques como guía si aplica."
    data = _blocks_to_data_dict(blocks)

    req = RenderRequest(
        doc_type=doc_type,
        template_skill_md=template_md,
        user_prompt=user_prompt,
        data=data,
        firm_id=firm_id,
        user_id=doc_meta.get("user_id"),
        matter_id=doc_meta.get("matter_id"),
        document_id=document_id,
    )

    try:
        result = await render_docx(req)
    except Exception as e:
        # Auditar el fallo y devolver None para que el caller use el legacy
        try:
            await log_audit(pool, req=req, error=e, fallback_used=True, fallback_reason=str(e)[:300])
        except Exception:
            pass
        logger.warning("Claude render failed (%s): %s", type(e).__name__, e)
        return None

    # Auditar éxito
    try:
        await log_audit(pool, req=req, result=result)
    except Exception:
        pass
    return result.docx_bytes


async def _load_template_skill_md(pool, *, doc_type: str, firm_id: str | None) -> str | None:
    """Resuelve SKILL.md del template via cascade firm → builtin.

    Busca en firm_skills.frontmatter->>'doc_type' = $doc_type. Si existe custom
    de la firma (firm_id no nulo y status=published) → ese. Sino builtin (firm_id NULL).
    """
    try:
        async with pool.acquire() as conn:
            # Custom de la firma
            if firm_id:
                row = await conn.fetchrow(
                    """
                    select system_prompt, references_md
                      from firm_skills
                     where firm_id = $1::uuid
                       and status = 'published'
                       and frontmatter->>'doc_type' = $2
                     order by version desc
                     limit 1
                    """,
                    firm_id, doc_type,
                )
                if row and row["system_prompt"]:
                    return _compose_template_md(row["system_prompt"], row["references_md"])

            # Builtin
            row = await conn.fetchrow(
                """
                select system_prompt, references_md
                  from firm_skills
                 where firm_id is null
                   and status = 'published'
                   and frontmatter->>'doc_type' = $1
                 order by version desc
                 limit 1
                """,
                doc_type,
            )
            if row and row["system_prompt"]:
                return _compose_template_md(row["system_prompt"], row["references_md"])
    except Exception as e:
        logger.debug("load_template_skill_md failed: %s", e)
    return None


def _compose_template_md(system_prompt: str, references_md: str | None) -> str:
    """Combina system_prompt + references_md en formato SKILL.md-like."""
    parts = [system_prompt or ""]
    if references_md:
        parts.append("\n\n---\n\n## References\n\n")
        parts.append(references_md)
    return "".join(parts).strip()


def _blocks_to_data_dict(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Extrae datos clave desde los bloques persistidos para alimentar el renderer."""
    data: dict[str, Any] = {
        "sections": [],
        "title": None,
    }
    for b in blocks or []:
        section_key = b.get("section_key")
        block_data = b.get("block_data") or {}
        block_type = b.get("block_type")
        if block_type == "heading" and not data["title"]:
            txt = (block_data.get("text") or "").strip()
            if txt:
                data["title"] = txt
        data["sections"].append({
            "section_key": section_key,
            "block_type": block_type,
            "block_data": block_data,
        })
    return data


class BlockPatchBody(BaseModel):
    """M19.16.B1 — body para PATCH inline edit de un bloque."""
    block_data: dict[str, Any] = Field(..., description="Nuevo block_data completo (mismo shape que en BD)")
    # Opcional: si solo cambia el texto, el cliente puede mandar solo runs y el
    # servidor hace merge sobre el block_data existente.
    runs_only: bool = Field(default=False, description="Si true, solo reemplaza el campo `runs` del bloque actual")


@router.patch("/documents/{document_id}/blocks/{block_id}")
async def patch_block(
    document_id: str,
    block_id: str,
    body: BlockPatchBody,
    _claims: dict = Depends(_require_session),
):
    """M19.16 — Edit inline de un bloque (Harvey-style).

    Flujo:
      1. blocks_repo.update_block() (o replace_block_runs si runs_only)
      2. docx_storage.invalidate_cache() para forzar rebuild en próxima descarga
      3. versions_repo.create_version(change_type='user_edit', snapshot=blocks)

    Returns: {block: <updated row>, version: <id, num>}
    """
    if not _flag_enabled():
        raise HTTPException(status_code=503, detail="docgen_v2_disabled")
    storage = await get_storage()
    from lex.storage import BlocksRepo
    from lex.storage.versions_repo import VersionsRepo
    from lex.storage.docx_storage import invalidate_cache as docx_invalidate_cache

    repo = BlocksRepo(storage.pool)
    versions_repo = VersionsRepo(storage.pool)

    # 1) Persistir cambio
    if body.runs_only:
        runs = body.block_data.get("runs", [])
        if not isinstance(runs, list):
            raise HTTPException(status_code=400, detail="runs_only requires block_data.runs list")
        updated = await repo.replace_block_runs(document_id, block_id, runs)
    else:
        updated = await repo.update_block(document_id, block_id, body.block_data)

    if updated is None:
        raise HTTPException(status_code=404, detail="block_not_found")

    # 2) Invalidar cache DOCX (best-effort, no bloquea)
    try:
        await docx_invalidate_cache(storage.pool, document_id)
    except Exception as e:
        logger.debug("docx cache invalidate failed (non-fatal): %s", e)

    # 3) Snapshot version (best-effort; si falla el edit ya está aplicado)
    version_info: dict[str, Any] | None = None
    try:
        all_blocks = await repo.get_blocks_for_document(document_id)
        version_info = await versions_repo.create_version(
            document_id=document_id,
            change_type="user_edit",
            blocks_snapshot=all_blocks,
            section_key=updated.get("section_key"),
            feedback=f"inline edit on block {block_id}",
        )
    except Exception as e:
        logger.warning("version snapshot failed after PATCH (non-fatal): %s", e)

    return {
        "block": updated,
        "version": version_info,
        "cache_invalidated": True,
    }


class AuditChangeBody(BaseModel):
    """M19.17.C — body para auditar un cambio recién aplicado al documento."""
    edited_block_id: str = Field(..., description="block_id del bloque que se modificó")
    before_block_data: dict[str, Any] | None = Field(
        default=None,
        description="Snapshot del block_data ANTES del cambio (opcional; el frontend lo tiene)"
    )
    user_instruction: str = Field(
        default="",
        max_length=2000,
        description="Mensaje/instrucción del usuario que motivó el cambio. Vacío si fue edit inline directo."
    )


@router.post("/documents/{document_id}/audit-change")
async def audit_change_endpoint(
    document_id: str,
    body: AuditChangeBody,
    _claims: dict = Depends(_require_session),
):
    """M19.17.C — Audita un cambio recién aplicado en 8 dimensiones jurídicas.

    El frontend llama a este endpoint DESPUÉS de que un edit se haya persistido
    (PATCH /blocks/{id}, chat edit, etc.). El stage `change_auditor` corre un
    LLM-as-judge entrenado como abogado litigante senior colombiano.

    Returns: {coherence_score, summary, findings: [...]}
    """
    if not _flag_enabled():
        raise HTTPException(status_code=503, detail="docgen_v2_disabled")

    storage = await get_storage()
    from lex.storage import BlocksRepo
    from lex.orchestrator.stages.change_auditor import audit_change

    repo = BlocksRepo(storage.pool)
    all_blocks = await repo.get_blocks_for_document(document_id)
    if not all_blocks:
        raise HTTPException(status_code=404, detail="document_not_found_or_empty")

    # Localizar bloque editado en el estado actual (después del cambio)
    after_block = next(
        (b for b in all_blocks if b.get("block_id") == body.edited_block_id),
        None
    )
    if after_block is None:
        raise HTTPException(status_code=404, detail="edited_block_not_found")

    # Reconstruir "before" si el frontend lo mandó; si no, queda None y el auditor
    # solo verá el estado actual + la instrucción
    before_block = None
    if body.before_block_data:
        before_block = {
            "block_id": body.edited_block_id,
            "block_data": body.before_block_data,
            "block_type": body.before_block_data.get("type", "paragraph"),
        }

    client = get_openai_client()
    result = await audit_change(
        client=client,
        all_blocks=all_blocks,
        edited_block_id=body.edited_block_id,
        before_block=before_block,
        after_block=after_block,
        user_instruction=body.user_instruction,
    )
    return result


@router.get("/health")
async def health():
    """Health check + flag status."""
    return {
        "status": "ok",
        "flag_docgen_v2": _flag_enabled(),
        "version": "v2.0.0-M5",
    }
