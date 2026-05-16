"""Sprint E · Router /v1/skills · listar + ejecutar skills."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm
from utils.skill_loader import list_active_skills
from utils.skill_runner import run_skill

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/skills", tags=["skills"])


@router.get("")
async def list_skills(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    return await list_active_skills(storage.pool, principal.firm_id)


class ExecuteSkillBody(BaseModel):
    command: str = Field(..., min_length=1)
    input: dict[str, Any] = Field(default_factory=dict)
    matter_id: Optional[UUID] = None
    document_id: Optional[UUID] = None


@router.post("/execute")
async def execute_skill(
    body: ExecuteSkillBody,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    result = await run_skill(
        storage.pool,
        firm_id=principal.firm_id,
        user_id=principal.user_id,
        command=body.command,
        input_data=body.input,
        matter_id=str(body.matter_id) if body.matter_id else None,
        document_id=str(body.document_id) if body.document_id else None,
    )
    if not result.get("ok"):
        if result.get("error") == "blocked_by_hook":
            raise HTTPException(409, detail=result)
        if result.get("error") == "skill_not_found":
            raise HTTPException(404, detail=result)
        raise HTTPException(502, detail=result)
    return result


@router.get("/executions")
async def list_executions(
    matter_id: Optional[UUID] = None,
    command: Optional[str] = None,
    limit: int = 50,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        if matter_id:
            rows = await conn.fetch(
                """
                select id, command, skill_id, status, duration_ms,
                       tokens_input, tokens_output, started_at, completed_at,
                       hooks_fired, error_message
                  from skill_executions
                 where firm_id = $1::uuid and matter_id = $2::uuid
                 order by started_at desc limit $3
                """,
                principal.firm_id, matter_id, limit,
            )
        elif command:
            rows = await conn.fetch(
                """
                select id, command, skill_id, status, duration_ms,
                       tokens_input, tokens_output, started_at, completed_at,
                       hooks_fired, error_message
                  from skill_executions
                 where firm_id = $1::uuid and command = $2
                 order by started_at desc limit $3
                """,
                principal.firm_id, command, limit,
            )
        else:
            rows = await conn.fetch(
                """
                select id, command, skill_id, status, duration_ms,
                       tokens_input, tokens_output, started_at, completed_at,
                       hooks_fired, error_message
                  from skill_executions
                 where firm_id = $1::uuid
                 order by started_at desc limit $2
                """,
                principal.firm_id, limit,
            )
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        if d.get("skill_id"):
            d["skill_id"] = str(d["skill_id"])
        if d.get("started_at"):
            d["started_at"] = d["started_at"].isoformat()
        if d.get("completed_at"):
            d["completed_at"] = d["completed_at"].isoformat()
        out.append(d)
    return out
