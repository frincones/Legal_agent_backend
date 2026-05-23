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


async def _require_session(request: Request) -> dict[str, Any]:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing_bearer_token")
    return {"token": auth[7:]}


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
async def export_forensic_docx(
    document_id: str,
    _claims: dict = Depends(_require_session),
):
    """Exporta documento como .docx forense (Times NR, márgenes 3cm, justified)
    construyendo imperativamente desde bloques persistidos."""
    if not _flag_enabled():
        raise HTTPException(status_code=503, detail="docgen_v2_disabled")
    storage = await get_storage()
    from lex.storage import BlocksRepo
    from lex.docx_forensic_builder import build_docx_from_blocks
    from fastapi.responses import StreamingResponse
    import io as _io

    repo = BlocksRepo(storage.pool)
    blocks = await repo.get_blocks_for_document(document_id)
    if not blocks:
        raise HTTPException(status_code=404, detail="document_not_found_or_empty")

    try:
        docx_bytes = build_docx_from_blocks(blocks, title=f"Documento {document_id[:8]}", author="LexAI")
    except Exception as e:
        logger.exception("docx forensic export failed")
        raise HTTPException(status_code=500, detail=f"export_error:{str(e)[:120]}")

    return StreamingResponse(
        _io.BytesIO(docx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="documento_{document_id[:8]}.docx"'},
    )


@router.get("/health")
async def health():
    """Health check + flag status."""
    return {
        "status": "ok",
        "flag_docgen_v2": _flag_enabled(),
        "version": "v2.0.0-M5",
    }
