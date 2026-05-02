"""Chat endpoint: main conversational interface."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agent.base_agent import RAGAgent
from config.schema import load_config
from models.search import IntentType
from retrieval.intent_router import classify_intent
from utils.db import get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

# Single shared agent instance (stateless — session_id is passed per request)
_agent: Optional[RAGAgent] = None


async def _get_agent() -> RAGAgent:
    """Get or create the global agent instance."""
    global _agent
    if _agent is None:
        config = load_config()
        storage = await get_storage(config.storage)
        _agent = RAGAgent(config, storage)
        logger.info(
            "Agent initialized: %s (%s)",
            config.agent.name,
            config.agent.role,
        )
    return _agent


async def _classify(message: str) -> IntentType:
    """Classify user intent via LLM-based intent_router.

    Fast-path obvious greetings skip the LLM call (~2ms).
    Otherwise ~300ms gpt-4o-mini call. Defaults to KNOWLEDGE on any failure.
    """
    config = load_config()
    model = config.retrieval.intent_router.model or "gpt-4o-mini"
    db_tables = getattr(config.agent, "db_tables_schema", None)
    return await classify_intent(message, model=model, db_tables_schema=db_tables)


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    stream: bool = False


class ChatResponse(BaseModel):
    response: str
    intent: Optional[str] = None
    sources: list = []
    session_id: str = ""


@router.post("/", response_model=None)
async def chat(request: ChatRequest):
    """Send a message and get a response (JSON or streaming)."""
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    agent = await _get_agent()
    session_id = request.session_id or RAGAgent.new_session_id()

    intent = await _classify(request.message)
    is_conversation = intent == IntentType.CONVERSATION

    if is_conversation:
        logger.info("Intent=CONVERSATION, routing to chitchat: %r", request.message[:60])

    if request.stream:
        async def stream_generator():
            if is_conversation:
                async for chunk in agent.chat_chitchat_stream(
                    request.message, session_id=session_id
                ):
                    yield chunk
            else:
                async for chunk in agent.chat_stream(
                    request.message, session_id=session_id
                ):
                    yield chunk

        return StreamingResponse(
            stream_generator(),
            media_type="application/x-ndjson",
            headers={
                "X-Session-Id": session_id,
                "X-Intent": intent.value,
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    if is_conversation:
        result = await agent.chat_chitchat(request.message, session_id=session_id)
    else:
        result = await agent.chat(request.message, session_id=session_id)

    return ChatResponse(
        response=result["response"],
        intent=result.get("intent") or intent.value,
        sources=result.get("sources", []),
        session_id=result["session_id"],
    )


@router.post("/reset")
async def reset_session():
    """Generate a fresh session ID for a new conversation."""
    new_id = RAGAgent.new_session_id()
    return {"status": "reset", "new_session_id": new_id}


@router.post("/attach")
async def attach_file(
    file: UploadFile = File(...),
    session_id: str = Form(...),
):
    """Upload and ingest a file to be used in the current chat session."""
    from ingestion.pipeline import IngestionPipeline
    from utils.db import get_storage
    from config.schema import load_config
    import uuid
    from pathlib import Path

    config = load_config()
    storage = await get_storage(config.storage)

    # Save file temporarily
    upload_dir = Path(".cache/uploads")
    upload_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename or "file.txt").suffix
    temp_path = upload_dir / f"{uuid.uuid4()}{ext}"

    content = await file.read()
    temp_path.write_bytes(content)

    try:
        # Ingest synchronously (user waits)
        pipeline = IngestionPipeline(config, storage)
        doc_id = await pipeline.ingest_file(
            str(temp_path),
            original_filename=file.filename,
        )

        if not doc_id:
            raise HTTPException(400, "Could not process file")

        # Get chunk count
        docs = await storage.list_documents()
        chunk_count = 0
        for d in docs:
            if d["id"] == doc_id:
                chunk_count = d.get("chunk_count", 0)
                break

        # Save attachment reference
        await storage.save_chat_attachment(session_id, doc_id, file.filename or "file", chunk_count)

        # Invalidate doc cache in agent
        agent = await _get_agent()
        agent.invalidate_doc_cache()

        return {
            "status": "ok",
            "doc_id": doc_id,
            "filename": file.filename,
            "chunk_count": chunk_count,
        }
    except Exception as e:
        logger.error(f"File attach failed: {e}")
        raise HTTPException(500, f"Error processing file: {str(e)}")
    finally:
        temp_path.unlink(missing_ok=True)
