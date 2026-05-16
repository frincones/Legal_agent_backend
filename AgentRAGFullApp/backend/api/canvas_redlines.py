"""Sprint E · Router /v1/canvas/redlines · gestionar redlines pending/applied/rejected."""

from __future__ import annotations

import json
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm
from utils.redline_diff import apply_redlines

router = APIRouter(prefix="/v1/canvas/redlines", tags=["canvas-redlines"])


class CreateRedlinesBody(BaseModel):
    matter_id: Optional[UUID] = None
    document_id: Optional[UUID] = None
    canvas_session_id: Optional[UUID] = None
    redlines: list[dict] = Field(..., min_length=1)
    original_text: str
    source_skill_id: Optional[UUID] = None


@router.post("")
async def create_redlines(
    body: CreateRedlinesBody,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into canvas_redlines
              (firm_id, matter_id, document_id, canvas_session_id, source_skill_id,
               redlines, original_text, created_by)
            values ($1::uuid, $2, $3, $4, $5,
                    $6::jsonb, $7, $8::uuid)
            returning id, status, created_at
            """,
            principal.firm_id, body.matter_id, body.document_id,
            body.canvas_session_id, body.source_skill_id,
            json.dumps(body.redlines), body.original_text, principal.user_id,
        )
    return {
        "id": str(row["id"]),
        "status": row["status"],
        "created_at": row["created_at"].isoformat(),
        "redline_count": len(body.redlines),
    }


@router.get("")
async def list_redlines(
    matter_id: Optional[UUID] = None,
    document_id: Optional[UUID] = None,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        if document_id:
            rows = await conn.fetch(
                """select id, matter_id, document_id, redlines, original_text,
                          result_text, status, applied_count, rejected_count,
                          created_at, applied_at
                     from canvas_redlines
                    where firm_id = $1::uuid and document_id = $2::uuid
                    order by created_at desc""",
                principal.firm_id, document_id,
            )
        elif matter_id:
            rows = await conn.fetch(
                """select id, matter_id, document_id, redlines, original_text,
                          result_text, status, applied_count, rejected_count,
                          created_at, applied_at
                     from canvas_redlines
                    where firm_id = $1::uuid and matter_id = $2::uuid
                    order by created_at desc""",
                principal.firm_id, matter_id,
            )
        else:
            rows = await conn.fetch(
                """select id, matter_id, document_id, redlines, original_text,
                          result_text, status, applied_count, rejected_count,
                          created_at, applied_at
                     from canvas_redlines
                    where firm_id = $1::uuid
                    order by created_at desc limit 50""",
                principal.firm_id,
            )
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        if d.get("matter_id"):
            d["matter_id"] = str(d["matter_id"])
        if d.get("document_id"):
            d["document_id"] = str(d["document_id"])
        if isinstance(d.get("redlines"), str):
            try:
                d["redlines"] = json.loads(d["redlines"])
            except Exception:
                d["redlines"] = []
        for k in ("created_at", "applied_at"):
            if d.get(k):
                d[k] = d[k].isoformat()
        out.append(d)
    return out


class ApplyBody(BaseModel):
    accept_ids: list[str] = Field(default_factory=list)
    reject_ids: list[str] = Field(default_factory=list)


@router.post("/{redline_set_id}/apply")
async def apply_redline_set(
    redline_set_id: UUID,
    body: ApplyBody,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """select redlines, original_text, status
                 from canvas_redlines
                where id = $1::uuid and firm_id = $2::uuid""",
            redline_set_id, principal.firm_id,
        )
    if not row:
        raise HTTPException(404, "redline set not found")
    if row["status"] not in ("pending", "applied"):
        raise HTTPException(409, f"Cannot apply · status={row['status']}")

    redlines = row["redlines"]
    if isinstance(redlines, str):
        redlines = json.loads(redlines)

    accept_set = set(body.accept_ids)
    result_text = apply_redlines(row["original_text"], redlines, only_ids=accept_set)

    async with storage.pool.acquire() as conn:
        await conn.execute(
            """update canvas_redlines
                  set status = 'applied',
                      applied_count = $1,
                      rejected_count = $2,
                      result_text = $3,
                      applied_by = $4::uuid,
                      applied_at = now()
                where id = $5::uuid""",
            len(accept_set), len(body.reject_ids),
            result_text, principal.user_id, redline_set_id,
        )

    return {
        "id": str(redline_set_id),
        "status": "applied",
        "applied_count": len(accept_set),
        "rejected_count": len(body.reject_ids),
        "result_text": result_text,
    }


@router.post("/{redline_set_id}/reject-all")
async def reject_all(
    redline_set_id: UUID,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        r = await conn.fetchrow(
            """update canvas_redlines
                  set status = 'rejected',
                      applied_count = 0,
                      rejected_count = jsonb_array_length(redlines),
                      result_text = original_text,
                      applied_by = $1::uuid,
                      applied_at = now()
                where id = $2::uuid and firm_id = $3::uuid
              returning id""",
            principal.user_id, redline_set_id, principal.firm_id,
        )
    if not r:
        raise HTTPException(404, "redline set not found")
    return {"id": str(r["id"]), "status": "rejected"}
