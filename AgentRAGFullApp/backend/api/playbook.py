"""Sprint E · Router /v1/firm/playbook · CRUD del playbook."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm
from utils.playbook_resolver import get_firm_playbook, upsert_firm_playbook

router = APIRouter(prefix="/v1/firm", tags=["playbook"])


class PlaybookUpdate(BaseModel):
    jurisdiction_default: Optional[str] = "co"
    redline_style: Optional[str] = "tracked"
    tone: Optional[str] = "formal"
    preferred_clauses: dict[str, str] = Field(default_factory=dict)
    forbidden_terms: list[str] = Field(default_factory=list)
    required_clauses: list[str] = Field(default_factory=list)
    escalation_matrix: list[dict[str, Any]] = Field(default_factory=list)
    raw_md: Optional[str] = None


@router.get("/playbook")
async def get_playbook(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    return await get_firm_playbook(storage.pool, principal.firm_id)


@router.put("/playbook")
async def update_playbook(
    body: PlaybookUpdate,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    return await upsert_firm_playbook(
        storage.pool, principal.firm_id, principal.user_id,
        body.model_dump(),
    )
