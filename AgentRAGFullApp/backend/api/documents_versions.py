"""Sprint M6 · Router para versiones + regenerate-section."""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from lex.storage import BlocksRepo
from lex.storage.versions_repo import VersionsRepo
from utils.db import get_storage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/documents/v2", tags=["documents-v2-versions"])


def _flag_enabled() -> bool:
    return os.getenv("FLAG_DOCGEN_V2", "false").lower() in ("1", "true", "yes")


async def _require_session(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing_bearer_token")
    return {"token": auth[7:]}


class RegenerateSectionBody(BaseModel):
    section_key: str
    feedback: Optional[str] = None


@router.post("/documents/{document_id}/regenerate-section")
async def regenerate_section(
    document_id: str,
    body: RegenerateSectionBody,
    _claims: dict = Depends(_require_session),
):
    """Regenera una sección puntual con feedback opcional.

    En M6 mínimo: marca la versión anterior + retorna 202 (la regeneración
    real es disparada por el frontend al iniciar nuevo SSE filtrado por section).
    En M7+ se implementa regeneración server-side con orchestrator parcial.
    """
    if not _flag_enabled():
        raise HTTPException(status_code=503, detail="docgen_v2_disabled")
    storage = await get_storage()
    repo = BlocksRepo(storage.pool)
    versions_repo = VersionsRepo(storage.pool)

    # Snapshot actual antes de borrar
    current = await repo.get_blocks_for_document(document_id)
    if not current:
        raise HTTPException(status_code=404, detail="document_not_found")

    # Crear versión "antes de regenerar"
    ver = await versions_repo.create_version(
        document_id=document_id,
        change_type="regenerate_section",
        section_key=body.section_key,
        blocks_snapshot=current,
        feedback=body.feedback,
    )

    # Borrar bloques de esa sección (orquestador frontend re-stream la sección)
    deleted = await repo.delete_section_blocks(document_id, body.section_key)

    return {
        "document_id": document_id,
        "section_key": body.section_key,
        "snapshot_version": ver,
        "deleted_blocks": deleted,
        "next_action": "frontend should POST /v1/documents/v2/generate with same intent + doc_type to re-stream only this section (M7 will do server-side).",
    }


@router.get("/documents/{document_id}/versions")
async def list_versions(
    document_id: str,
    _claims: dict = Depends(_require_session),
):
    if not _flag_enabled():
        raise HTTPException(status_code=503, detail="docgen_v2_disabled")
    storage = await get_storage()
    repo = VersionsRepo(storage.pool)
    versions = await repo.list_versions(document_id)
    return {"document_id": document_id, "versions": versions, "total": len(versions)}


@router.get("/documents/{document_id}/versions/{version_num}")
async def get_version(
    document_id: str,
    version_num: int,
    _claims: dict = Depends(_require_session),
):
    if not _flag_enabled():
        raise HTTPException(status_code=503, detail="docgen_v2_disabled")
    storage = await get_storage()
    repo = VersionsRepo(storage.pool)
    snapshot = await repo.get_version_snapshot(document_id, version_num)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="version_not_found")
    return {
        "document_id": document_id,
        "version_num": version_num,
        "blocks": snapshot,
        "total": len(snapshot),
    }


@router.get("/documents/{document_id}/versions/{from_v}/diff/{to_v}")
async def diff_versions(
    document_id: str,
    from_v: int,
    to_v: int,
    _claims: dict = Depends(_require_session),
):
    if not _flag_enabled():
        raise HTTPException(status_code=503, detail="docgen_v2_disabled")
    storage = await get_storage()
    repo = VersionsRepo(storage.pool)
    return await repo.diff_versions(document_id, from_v, to_v)
