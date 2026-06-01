"""Sprint M21.S2.F · Cold-start interview API (LexAI = Claude-for-Legal parity).

Endpoints (firm-scoped, requieren auth):
  POST   /v2/onboarding/cold-start/start              · crea session (o reusa in_progress)
  POST   /v2/onboarding/cold-start/answer             · graba respuestas de la parte actual
  GET    /v2/onboarding/cold-start/{session_id}       · status + respuestas acumuladas
  POST   /v2/onboarding/cold-start/{session_id}/finish  · persiste firms_profile + practice areas
  POST   /v2/onboarding/cold-start/{session_id}/abandon · marca abandoned

  POST   /v2/onboarding/seed-docs/upload              · sube doc modelo a firm_seed_documents
  GET    /v2/onboarding/seed-docs                     · lista seed docs del firm
  DELETE /v2/onboarding/seed-docs/{doc_id}            · elimina seed doc

  GET    /v2/onboarding/company-profile               · GET firms_profile actual
  GET    /v2/onboarding/practice-areas                · GET practice_profile_sections

NO sustituye api/onboarding.py (Sprint 26 — checklist). Ambos coexisten.

Delega en lex/tools/cold_start_interview.py para la logica del state-machine.
"""

from __future__ import annotations

import base64
import logging
from typing import Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm
from utils.db import get_storage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v2/onboarding", tags=["onboarding-cold-start"])


# ─── Helper: invocar tool cold_start_interview ─────────────────────

async def _invoke_cold_start(
    principal: Principal, action: str,
    session_id: Optional[str] = None, answers: Optional[dict] = None,
) -> dict:
    """Llama directamente al tool (sin pasar por el Brain, mas eficiente)."""
    from lex.tools.base import ToolContext
    from lex.tools.cold_start_interview import ColdStartInterviewTool

    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")

    ctx = ToolContext(
        generation_id=uuid4(),
        firm_id=UUID(principal.firm_id) if isinstance(principal.firm_id, str) else principal.firm_id,
        user_id=UUID(principal.user_id) if isinstance(principal.user_id, str) and principal.user_id else None,
        pool=pool,
    )
    tool = ColdStartInterviewTool(pool=pool)
    try:
        return await tool.run(ctx, action=action, session_id=session_id, answers=answers)
    except Exception as e:
        logger.exception("cold_start_interview tool failed action=%s", action)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


# ─── Cold-start state machine ─────────────────────────────────────

@router.post("/cold-start/start")
async def cold_start_start(principal: Principal = Depends(get_current_firm)):
    """Inicia cold-start interview. Reusa session in_progress si existe."""
    return await _invoke_cold_start(principal, "start")


class AnswerBody(BaseModel):
    session_id: str = Field(min_length=8)
    answers: dict


@router.post("/cold-start/answer")
async def cold_start_answer(body: AnswerBody, principal: Principal = Depends(get_current_firm)):
    """Graba respuestas de la parte actual y avanza a la siguiente."""
    return await _invoke_cold_start(principal, "answer", body.session_id, body.answers)


@router.get("/cold-start/{session_id}")
async def cold_start_status(session_id: str, principal: Principal = Depends(get_current_firm)):
    return await _invoke_cold_start(principal, "status", session_id)


@router.post("/cold-start/{session_id}/finish")
async def cold_start_finish(session_id: str, principal: Principal = Depends(get_current_firm)):
    """Persiste firms_profile + practice_profile_sections + mark completed."""
    return await _invoke_cold_start(principal, "finish", session_id)


@router.post("/cold-start/{session_id}/abandon")
async def cold_start_abandon(session_id: str, principal: Principal = Depends(get_current_firm)):
    return await _invoke_cold_start(principal, "abandon", session_id)


# ─── Seed documents ──────────────────────────────────────────────

class SeedDocUploadBody(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    area: Optional[str] = Field(None, max_length=80)
    doc_type: Optional[str] = Field(None, max_length=80)
    content_base64: str = Field(description="Bytes del documento en base64")
    content_mime: str = Field(default="application/pdf", max_length=120)
    notes_md: Optional[str] = None


@router.post("/seed-docs/upload")
async def seed_docs_upload(
    body: SeedDocUploadBody,
    principal: Principal = Depends(get_current_firm),
):
    """Sube doc modelo. Maximo 10MB por archivo, 100 archivos por firm."""
    try:
        content_bytes = base64.b64decode(body.content_base64)
    except Exception as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"content_base64 invalido: {e}")
    if len(content_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "archivo >10MB")

    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")

    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "select count(*) from firm_seed_documents where firm_id = $1",
            str(principal.firm_id),
        )
        if (count or 0) >= 100:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                                "limite de 100 seed documents alcanzado")

        doc_id = await conn.fetchval(
            """
            insert into firm_seed_documents
                (firm_id, uploaded_by_user_id, title, area, doc_type,
                 content_bytes, content_mime, notes_md, status)
            values ($1, $2, $3, $4, $5, $6, $7, $8, 'pending')
            returning seed_doc_id
            """,
            str(principal.firm_id),
            str(principal.user_id) if principal.user_id else None,
            body.title, body.area, body.doc_type,
            content_bytes, body.content_mime, body.notes_md,
        )
    return {
        "seed_doc_id": str(doc_id),
        "title": body.title,
        "size_bytes": len(content_bytes),
        "status": "pending",
        "note": "Doc almacenado. Extraccion de patterns pendiente de Sprint 4 (background agent learn_from_examples).",
    }


@router.get("/seed-docs")
async def seed_docs_list(principal: Principal = Depends(get_current_firm)):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select seed_doc_id, title, area, doc_type, content_mime,
                   length(content_bytes) as size_bytes, status,
                   created_at, processed_at
              from firm_seed_documents
             where firm_id = $1
             order by created_at desc
             limit 100
            """,
            str(principal.firm_id),
        )
    return {
        "items": [
            {
                "seed_doc_id": str(r["seed_doc_id"]),
                "title": r["title"],
                "area": r["area"],
                "doc_type": r["doc_type"],
                "content_mime": r["content_mime"],
                "size_bytes": r["size_bytes"],
                "status": r["status"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "processed_at": r["processed_at"].isoformat() if r["processed_at"] else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.delete("/seed-docs/{doc_id}")
async def seed_docs_delete(doc_id: str, principal: Principal = Depends(get_current_firm)):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    async with pool.acquire() as conn:
        result = await conn.execute(
            "delete from firm_seed_documents where seed_doc_id = $1::uuid and firm_id = $2",
            doc_id, str(principal.firm_id),
        )
    deleted = result and result.endswith(" 1")
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "seed doc no encontrado")
    return {"ok": True, "seed_doc_id": doc_id}


# ─── Company / practice profile read endpoints ─────────────────────

@router.get("/company-profile")
async def get_company_profile(principal: Principal = Depends(get_current_firm)):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select firm_id, company_name, legal_name, nit, industry,
                   practice_setting, jurisdiction, size_employees,
                   pain_points_md, metadata, created_at, updated_at
              from firms_profile
             where firm_id = $1 limit 1
            """,
            str(principal.firm_id),
        )
    if not row:
        return {"_exists": False, "message": "Firma no completo cold-start"}
    return {
        "_exists": True,
        "firm_id": str(row["firm_id"]),
        "company_name": row["company_name"],
        "legal_name": row["legal_name"],
        "nit": row["nit"],
        "industry": row["industry"],
        "practice_setting": row["practice_setting"],
        "jurisdiction": row["jurisdiction"],
        "size_employees": row["size_employees"],
        "pain_points_md": row["pain_points_md"],
        "metadata": dict(row["metadata"] or {}),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.get("/practice-areas")
async def get_practice_areas(principal: Principal = Depends(get_current_firm)):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select section_id, area, is_primary, profile_md, metadata,
                   created_at, updated_at
              from practice_profile_sections
             where firm_id = $1
             order by is_primary desc, area
            """,
            str(principal.firm_id),
        )
    return {
        "items": [
            {
                "section_id": str(r["section_id"]),
                "area": r["area"],
                "is_primary": r["is_primary"],
                "profile_md": r["profile_md"],
                "metadata": dict(r["metadata"] or {}),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }
