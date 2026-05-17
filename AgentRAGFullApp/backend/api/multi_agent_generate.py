"""Multi-agent document generation endpoint.

  POST /v1/multi-agent/generate           · sync (waits, returns final report)
  POST /v1/multi-agent/generate/stream    · SSE · streams each stage event

These are the user-facing endpoints for the Sprint 3 multi-agent
orchestrator (`agent/workers/document_generator.py`). They do NOT
replace `/v1/skills/execute/stream` · the single-shot skill runner
continues to work unchanged.

Request body:
    {
      "materia": "laboral",            # materia_legal enum
      "doc_kind": "demanda_laboral",   # doc_kind label
      "user_brief": "...",             # natural language brief
      "template_id": "uuid|null",      # optional · system or firm template
      "matter_id": "uuid|null",
      "channel": "voice|chat|cmdk|api",
      "initial_slots": {...}           # optional override slots
    }
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/multi-agent", tags=["multi_agent"])


class GenerateIn(BaseModel):
    materia: str = Field(..., min_length=2, max_length=40)
    doc_kind: str = Field(..., min_length=2, max_length=40)
    user_brief: str = Field(..., min_length=4, max_length=8000)
    template_id: Optional[UUID] = None
    matter_id: Optional[UUID] = None
    channel: str = Field("chat", pattern="^(voice|chat|cmdk|api)$")
    initial_slots: Optional[dict[str, Any]] = None


@router.post("/generate")
async def generate(
    body: GenerateIn,
    principal: Principal = Depends(get_current_firm),
):
    """Synchronous generation · returns the final report (no streaming).

    Useful for cmdk one-shot calls. Voice and chat should use /stream.
    """
    from agent.workers.document_generator import run_document_generator
    from utils.db import get_storage

    storage = await get_storage()
    final_event: Optional[dict[str, Any]] = None
    all_events: list[dict[str, Any]] = []

    try:
        async for ev in run_document_generator(
            firm_id=principal.firm_id,
            user_id=principal.user_id,
            matter_id=str(body.matter_id) if body.matter_id else None,
            channel=body.channel,
            template_id=str(body.template_id) if body.template_id else None,
            materia=body.materia,
            doc_kind=body.doc_kind,
            user_brief=body.user_brief,
            pool=storage.pool,
            initial_slots=body.initial_slots,
        ):
            all_events.append(ev)
            if ev["event"] == "ready_to_send":
                final_event = ev
    except Exception as e:
        logger.exception("multi_agent generate failed: %s", e)
        raise HTTPException(500, detail=str(e)[:240])

    if not final_event:
        raise HTTPException(500, detail="generation completed without ready_to_send")

    return {
        "ok": True,
        "report": final_event["data"],
        "event_count": len(all_events),
    }


@router.post("/generate/stream")
async def generate_stream(
    body: GenerateIn,
    principal: Principal = Depends(get_current_firm),
):
    """SSE streaming · each stage event is yielded as `event: <name>` frames.

    Same event names as the orchestrator. The frontend AssistantSidebar
    listens for `ready_to_send` to open the HITL gate.
    """
    from agent.workers.document_generator import run_document_generator
    from utils.db import get_storage

    storage = await get_storage()

    async def event_generator():
        try:
            async for ev in run_document_generator(
                firm_id=principal.firm_id,
                user_id=principal.user_id,
                matter_id=str(body.matter_id) if body.matter_id else None,
                channel=body.channel,
                template_id=str(body.template_id) if body.template_id else None,
                materia=body.materia,
                doc_kind=body.doc_kind,
                user_brief=body.user_brief,
                pool=storage.pool,
                initial_slots=body.initial_slots,
            ):
                payload = json.dumps(ev["data"], ensure_ascii=False, default=str)
                yield f"event: {ev['event']}\ndata: {payload}\n\n"
        except Exception as e:
            logger.exception("multi_agent stream failed: %s", e)
            err = json.dumps({"error": "stream_failed", "detail": str(e)[:240]})
            yield f"event: error\ndata: {err}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "cache-control": "no-cache, no-transform",
            "x-accel-buffering": "no",
            "connection": "keep-alive",
        },
    )
