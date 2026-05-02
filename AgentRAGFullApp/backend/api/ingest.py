"""Ingestion endpoints: upload and process documents.

Files are processed in the background. The endpoint creates a 'pending'
document row immediately and returns. A background asyncio task does the
heavy work (read, chunk, embed, save) and updates the document's status
to 'completed' or 'failed'. The frontend polls /api/documents/ to track
progress.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from config.schema import load_config
from ingestion.pipeline import IngestionPipeline
from utils.db import get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["ingestion"])

# Persistent upload storage so files survive between API response and
# background task execution.
UPLOAD_DIR = Path(__file__).parent.parent / ".cache" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


class IngestTextRequest(BaseModel):
    text: str
    title: str = "Untitled"
    source: str = "api_input"
    doc_type: str = "text"


class IngestDirectoryRequest(BaseModel):
    directory: str
    clean_before: bool = False


@router.post("/text")
async def ingest_text(request: IngestTextRequest):
    """Ingest raw text directly into the knowledge base (synchronous)."""
    config = load_config()
    storage = await get_storage()
    pipeline = IngestionPipeline(config, storage)

    doc_id = await pipeline.ingest_text(
        text=request.text,
        title=request.title,
        source=request.source,
        doc_type=request.doc_type,
    )

    if doc_id is None:
        raise HTTPException(status_code=400, detail="Failed to ingest text")

    return {"status": "ingested", "document_id": doc_id}


async def _process_in_background(
    document_id: str,
    persistent_path: str,
    original_filename: str,
):
    """Background task: process the file and update the pending document."""
    try:
        config = load_config()
        storage = await get_storage()
        pipeline = IngestionPipeline(config, storage)

        await pipeline.process_into_existing_document(
            document_id=document_id,
            file_path=persistent_path,
            original_filename=original_filename,
        )
    finally:
        # Clean up the persistent upload file
        try:
            os.unlink(persistent_path)
        except Exception:
            pass


@router.post("/files")
async def ingest_files(files: List[UploadFile] = File(...)):
    """Upload one or more files for background ingestion.

    Returns immediately with the list of created (pending) document IDs.
    The frontend should poll /api/documents/ to see when status transitions
    to 'completed' or 'failed'.
    """
    storage = await get_storage()

    results = []
    for file in files:
        original_name = file.filename or "file.txt"

        try:
            # 1. Save file to a persistent location (survives the response)
            ext = os.path.splitext(original_name)[1]
            persistent_id = uuid.uuid4().hex
            persistent_path = UPLOAD_DIR / f"{persistent_id}{ext}"

            content = await file.read()
            persistent_path.write_bytes(content)

            # 2. Create a pending document row immediately
            doc_id = await storage.create_pending_document(
                title=Path(original_name).stem,
                source=original_name,
                doc_type="pending",
                metadata={
                    "original_filename": original_name,
                    "size_bytes": len(content),
                },
            )

            # 3. Schedule background task (returns immediately)
            asyncio.create_task(
                _process_in_background(
                    document_id=doc_id,
                    persistent_path=str(persistent_path),
                    original_filename=original_name,
                )
            )

            results.append({
                "file": original_name,
                "status": "queued",
                "document_id": doc_id,
            })
            logger.info("Queued '%s' for background ingestion (id=%s)", original_name, doc_id)

        except Exception as e:
            logger.error("Failed to queue %s: %s", original_name, e)
            results.append({
                "file": original_name,
                "status": "failed",
                "error": str(e),
            })

    queued = sum(1 for r in results if r["status"] == "queued")
    return {
        "total": len(files),
        "queued": queued,
        "failed": len(files) - queued,
        "results": results,
    }


@router.post("/directory")
async def ingest_directory(request: IngestDirectoryRequest):
    """Ingest all supported files from a directory (synchronous)."""
    if not os.path.isdir(request.directory):
        raise HTTPException(status_code=400, detail=f"Directory not found: {request.directory}")

    config = load_config()
    storage = await get_storage()
    pipeline = IngestionPipeline(config, storage)

    result = await pipeline.ingest_directory(
        directory=request.directory,
        clean_before=request.clean_before,
    )

    return result.to_dict()
