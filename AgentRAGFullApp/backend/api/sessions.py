"""Session management endpoints: list, get, delete chat sessions."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException

from utils.db import get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.get("/")
async def list_sessions():
    """List all chat sessions ordered by most recent first."""
    storage = await get_storage()
    sessions = await storage.list_sessions(limit=100)
    return {"sessions": sessions, "total": len(sessions)}


@router.get("/{session_id}")
async def get_session(session_id: str):
    """Retrieve all messages from a specific session."""
    storage = await get_storage()
    messages = await storage.get_session_messages(session_id)
    if not messages:
        raise HTTPException(status_code=404, detail="Session not found or empty")
    return {"session_id": session_id, "messages": messages}


@router.delete("/{session_id}")
async def delete_session(session_id: str):
    """Delete a session and all its messages."""
    storage = await get_storage()
    deleted = await storage.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "deleted", "session_id": session_id}
