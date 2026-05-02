"""Document management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from utils.db import get_storage

router = APIRouter(prefix="/documents", tags=["documents"])


class IngestTextRequest(BaseModel):
    text: str
    title: str = "Untitled"
    source: str = "api_upload"
    doc_type: str = "text"


@router.get("/")
async def list_documents():
    """List all documents in the knowledge base."""
    storage = await get_storage()
    docs = await storage.list_documents()
    return {"documents": docs, "total": len(docs)}


@router.get("/{document_id}")
async def get_document(document_id: str):
    """Get a specific document by ID."""
    storage = await get_storage()
    doc = await storage.get_document(document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return {
        "id": doc.id,
        "title": doc.title,
        "source": doc.source,
        "doc_type": doc.doc_type,
        "content_preview": doc.content[:500] if doc.content else "",
        "word_count": doc.word_count,
        "metadata": doc.metadata,
    }


@router.delete("/{document_id}")
async def delete_document(document_id: str):
    """Delete a document and its chunks."""
    storage = await get_storage()
    deleted = await storage.delete_document(document_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"status": "deleted", "id": document_id}


@router.delete("/")
async def clear_all_documents():
    """Delete all documents and chunks from the knowledge base."""
    storage = await get_storage()
    await storage.clear_all()
    return {"status": "cleared"}
