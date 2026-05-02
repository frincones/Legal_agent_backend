"""F1 · matter_documents endpoints for deep IA analysis.

Separate from api/documents.py (which manages the RAG corpus).
This module exposes:
  POST /v1/matter-documents/{id}/analyze  · trigger extraction
  GET  /v1/matter-documents/{id}/analysis · last extraction
  GET  /v1/matter-documents/by-matter/{matter_id}/analyses · all per matter
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/matter-documents", tags=["matter-documents"])


@router.post("/{document_id}/analyze")
async def analyze_matter_document(
    document_id: str,
    regenerate: bool = False,
    principal: Principal = Depends(get_current_firm),
):
    """Trigger entity extraction on a matter document. Idempotent: returns
    cached result unless regenerate=true."""
    from agent.tools.document_analysis import extract_document_entities_tool
    ctx = {
        "firm_id": principal.firm_id,
        "user_id": principal.user_id,
        "session_id": "rest",
    }
    result = await extract_document_entities_tool(
        args={"document_id": document_id, "regenerate": regenerate},
        ctx=ctx,
    )
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])
    return result


@router.get("/{document_id}/analysis")
async def get_matter_document_analysis(
    document_id: str,
    principal: Principal = Depends(get_current_firm),
):
    """Return the latest completed extraction for a document."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, status, parties_jsonb, dates_jsonb, obligations_jsonb,
                   montos_jsonb, inconsistencies_jsonb, hechos_clave,
                   riesgos_legales, vacios_probatorios,
                   confidence_score, model_used, prompt_version, pages_processed,
                   extracted_at
            from document_extractions
            where matter_document_id = $1::uuid and firm_id = $2::uuid
              and status = 'completed'
            order by extracted_at desc
            limit 1
            """,
            document_id, principal.firm_id,
        )
    if not row:
        return {"status": "missing"}
    return {
        "id": str(row["id"]),
        "status": row["status"],
        "parties": _to_jsonb(row["parties_jsonb"]),
        "dates": _to_jsonb(row["dates_jsonb"]),
        "obligations": _to_jsonb(row["obligations_jsonb"]),
        "montos": _to_jsonb(row["montos_jsonb"]),
        "inconsistencies": _to_jsonb(row["inconsistencies_jsonb"]),
        "hechos_clave": row["hechos_clave"],
        "riesgos_legales": _to_jsonb(row["riesgos_legales"]),
        "vacios_probatorios": _to_jsonb(row["vacios_probatorios"]),
        "confidence_score": float(row["confidence_score"] or 0),
        "model_used": row["model_used"],
        "prompt_version": row["prompt_version"],
        "pages_processed": row["pages_processed"],
        "extracted_at": row["extracted_at"].isoformat(),
    }


@router.get("/by-matter/{matter_id}/analyses")
async def list_matter_analyses(
    matter_id: str,
    principal: Principal = Depends(get_current_firm),
):
    """List the latest extraction per document for a matter."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            with last_per_doc as (
              select de.*, row_number() over
                (partition by matter_document_id order by extracted_at desc) as rn
              from document_extractions de
              where de.matter_id = $1::uuid and de.firm_id = $2::uuid
            )
            select last_per_doc.*, md.titulo, md.kind
            from last_per_doc
            join matter_documents md on md.id = last_per_doc.matter_document_id
            where rn = 1
            order by extracted_at desc
            """,
            matter_id, principal.firm_id,
        )
    return {
        "count": len(rows),
        "analyses": [
            {
                "id": str(r["id"]),
                "matter_document_id": str(r["matter_document_id"]),
                "document_titulo": r["titulo"],
                "document_kind": r["kind"],
                "status": r["status"],
                "parties_count": len(_to_jsonb(r["parties_jsonb"])),
                "obligations_count": len(_to_jsonb(r["obligations_jsonb"])),
                "inconsistencies_count": len(_to_jsonb(r["inconsistencies_jsonb"])),
                "confidence_score": float(r["confidence_score"] or 0),
                "extracted_at": r["extracted_at"].isoformat(),
                "hechos_clave": r["hechos_clave"],
            }
            for r in rows
        ],
    }


def _to_jsonb(v):
    """asyncpg may return jsonb as str or dict; normalize."""
    if v is None:
        return []
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return []
    return v
