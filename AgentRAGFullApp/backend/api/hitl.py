"""HITL (Human-in-the-loop) gates · F5.

Flow:
  1. The voice agent calls tool `request_human_approval` with kind+preview.
  2. The Realtime relay (api/voice.py) calls request_approval_tool() here,
     which inserts an `hitl_interrupts` row and registers an asyncio.Future.
  3. The frontend renders the appropriate gate modal, lets the lawyer
     approve/edit/reject, then PATCHes /v1/hitl/{id}/decide.
  4. This router resolves the Future → the relay continues the conversation.

The 7 gate kinds match the PRD §5 F5 contract.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/hitl", tags=["hitl"])

# In-memory map of pending interrupts.
# In a multi-instance deployment we'd use Postgres LISTEN/NOTIFY or Redis
# pub/sub. For Sprint 1 single-instance Railway this is sufficient.
_pending: dict[str, asyncio.Future] = {}


class HITLPreview(BaseModel):
    """Loose payload — each gate kind has its own shape."""
    model_config = {"extra": "allow"}


class HITLDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approved|edited|rejected)$")
    decision_payload: Optional[dict] = None  # if 'edited', the lawyer's edits


class HITLInterruptRow(BaseModel):
    id: str
    firm_id: str
    user_id: str
    matter_id: Optional[str]
    kind: str
    payload: dict
    decision: str
    created_at: datetime
    decided_at: Optional[datetime] = None


# ─────────────────────────────────────────────────────────────────────
# REST endpoints
# ─────────────────────────────────────────────────────────────────────


@router.get("/", response_model=list[HITLInterruptRow])
async def list_pending(
    principal: Principal = Depends(get_current_firm),
    limit: int = 50,
):
    """Inbox of pending HITL gates for the current firm."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return []
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, firm_id, user_id, matter_id, kind, payload, decision,
                   created_at, decided_at
            from hitl_interrupts
            where firm_id = $1::uuid and decision = 'pending'
            order by created_at desc
            limit $2
            """,
            principal.firm_id, limit,
        )
    import json as _json
    out = []
    for r in rows:
        raw_payload = r["payload"]
        if isinstance(raw_payload, str):
            try:
                raw_payload = _json.loads(raw_payload)
            except Exception:
                raw_payload = {}
        if not isinstance(raw_payload, dict):
            raw_payload = {}
        out.append(HITLInterruptRow(
            id=str(r["id"]),
            firm_id=str(r["firm_id"]),
            user_id=str(r["user_id"]),
            matter_id=str(r["matter_id"]) if r["matter_id"] else None,
            kind=r["kind"],
            payload=raw_payload,
            decision=r["decision"],
            created_at=r["created_at"],
            decided_at=r["decided_at"],
        ))
    return out


@router.post("/{interrupt_id}/decide")
async def decide(
    interrupt_id: str,
    body: HITLDecisionRequest,
    principal: Principal = Depends(get_current_firm),
):
    """Resolve a pending HITL gate."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage not available")

    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update hitl_interrupts
            set decision = $1, decision_user_id = $2::uuid,
                decision_payload = $3::jsonb, decided_at = now()
            where id = $4::uuid and firm_id = $5::uuid and decision = 'pending'
            returning id, kind, decision, decision_payload
            """,
            body.decision,
            principal.user_id,
            __import__("json").dumps(body.decision_payload or {}),
            interrupt_id,
            principal.firm_id,
        )
    if not row:
        raise HTTPException(404, "interrupt not found or already decided")

    # Resolve any in-memory listener (the voice relay)
    fut = _pending.pop(interrupt_id, None)
    if fut and not fut.done():
        fut.set_result({
            "decision": body.decision,
            "decision_payload": body.decision_payload or {},
        })

    return {
        "id": interrupt_id,
        "decision": body.decision,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────
# Internal API (called by the voice relay)
# ─────────────────────────────────────────────────────────────────────


async def request_approval_tool(args: dict, ctx: dict) -> dict:
    """Tool implementation for `request_human_approval`.

    Persists an hitl_interrupts row, registers a Future, returns
    {pending: True, interrupt_id} so the relay can wait on it.
    """
    kind = args.get("kind", "")
    preview = args.get("preview") or {}
    if not kind:
        return {"error": "kind required"}

    interrupt_id = str(uuid.uuid4())
    try:
        from utils.db import get_storage
        storage = await get_storage()
        if hasattr(storage, "pool"):
            async with storage.pool.acquire() as conn:
                await conn.execute(
                    """
                    insert into hitl_interrupts
                      (id, firm_id, user_id, matter_id, kind, payload)
                    values
                      ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::hitl_kind, $6::jsonb)
                    """,
                    interrupt_id,
                    ctx.get("firm_id"),
                    ctx.get("user_id"),
                    ctx.get("matter_id"),
                    kind,
                    __import__("json").dumps(preview),
                )
    except Exception as e:
        logger.warning("HITL persist failed: %s", e)

    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    _pending[interrupt_id] = fut

    return {"pending": True, "interrupt_id": interrupt_id}


async def wait_for_decision(interrupt_id: str, timeout_s: float = 120.0) -> dict:
    """Block until the lawyer decides, or timeout."""
    fut = _pending.get(interrupt_id)
    if fut is None:
        return {"decision": "timeout", "error": "no listener"}
    try:
        return await asyncio.wait_for(fut, timeout=timeout_s)
    except asyncio.TimeoutError:
        _pending.pop(interrupt_id, None)
        # Mark as timeout in DB for audit
        try:
            from utils.db import get_storage
            storage = await get_storage()
            if hasattr(storage, "pool"):
                async with storage.pool.acquire() as conn:
                    await conn.execute(
                        "update hitl_interrupts set decision='timeout', decided_at=now() "
                        "where id=$1::uuid and decision='pending'",
                        interrupt_id,
                    )
        except Exception:
            pass
        return {"decision": "timeout"}
