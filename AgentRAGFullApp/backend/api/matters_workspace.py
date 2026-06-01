"""Sprint M21.S2.G · Matters Workspace API (LexAI v2).

Endpoints (firm-scoped, requieren auth):
  POST   /v2/matters                    · crea o switch matter (delega en register_matter tool)
  GET    /v2/matters                    · lista matters del firm
  GET    /v2/matters/{matter_id}        · detalle de un matter
  PATCH  /v2/matters/{matter_id}        · actualiza phase / theory / opposing_party
  POST   /v2/matters/{matter_id}/archive  · marca active=false
  GET    /v2/matters/{matter_id}/history  · timeline append-only de eventos

NOTA: NO sustituye api/matters.py (que usa tabla legacy `matters`). Ambos coexisten.
Esta API opera sobre `matters_workspace` (Sprint 1 schema, RLS multi-tenant).
"""

from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm
from utils.db import get_storage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v2/matters", tags=["matters-workspace"])


# ─── Schemas ────────────────────────────────────────────────────

class MatterCreateV2(BaseModel):
    title: str = Field(min_length=2, max_length=300)
    area: str = Field(min_length=2, max_length=80)
    side: Optional[str] = Field(None, max_length=40)
    slug: Optional[str] = Field(None, max_length=80)
    jurisdiction: str = Field(default="CO", max_length=8)
    phase: Optional[str] = Field(None, max_length=40)
    theory_md: Optional[str] = None
    opposing_party: Optional[str] = Field(None, max_length=300)
    client_name: Optional[str] = Field(None, max_length=300)
    switch_if_exists: bool = True


class MatterUpdateV2(BaseModel):
    title: Optional[str] = Field(None, min_length=2, max_length=300)
    phase: Optional[str] = Field(None, max_length=40)
    theory_md: Optional[str] = None
    opposing_party: Optional[str] = Field(None, max_length=300)
    client_name: Optional[str] = Field(None, max_length=300)


# ─── POST · create / switch ────────────────────────────────────

@router.post("", status_code=status.HTTP_201_CREATED)
async def create_matter(
    body: MatterCreateV2,
    principal: Principal = Depends(get_current_firm),
):
    from lex.tools.base import ToolContext
    from lex.tools.register_matter import RegisterMatterTool

    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")

    ctx = ToolContext(
        generation_id=uuid4(),
        firm_id=UUID(principal.firm_id) if isinstance(principal.firm_id, str) else principal.firm_id,
        user_id=UUID(principal.user_id) if isinstance(principal.user_id, str) and principal.user_id else None,
        pool=pool,
    )
    tool = RegisterMatterTool(pool=pool)
    try:
        return await tool.run(
            ctx,
            title=body.title, area=body.area, side=body.side, slug=body.slug,
            jurisdiction=body.jurisdiction, phase=body.phase, theory_md=body.theory_md,
            opposing_party=body.opposing_party, client_name=body.client_name,
            switch_if_exists=body.switch_if_exists,
        )
    except Exception as e:
        logger.exception("create_matter failed")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


# ─── GET · list ────────────────────────────────────────────────

@router.get("")
async def list_matters(
    principal: Principal = Depends(get_current_firm),
    area: Optional[str] = Query(None),
    active_only: bool = Query(True),
    limit: int = Query(50, le=200),
):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")

    where = ["firm_id = $1"]
    args: list = [str(principal.firm_id)]
    if active_only:
        where.append("active = true")
    if area:
        args.append(area)
        where.append(f"area = ${len(args)}")

    sql = f"""
        select matter_id, slug, title, area, side, jurisdiction, phase,
               opposing_party, client_name, active, created_at, updated_at
          from matters_workspace
         where {' and '.join(where)}
         order by updated_at desc nulls last, created_at desc
         limit {int(limit)}
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)

    return {
        "items": [
            {
                "matter_id": str(r["matter_id"]),
                "slug": r["slug"],
                "title": r["title"],
                "area": r["area"],
                "side": r["side"],
                "jurisdiction": r["jurisdiction"],
                "phase": r["phase"],
                "opposing_party": r["opposing_party"],
                "client_name": r["client_name"],
                "active": r["active"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }


# ─── GET · detail ──────────────────────────────────────────────

@router.get("/{matter_id}")
async def get_matter(matter_id: str, principal: Principal = Depends(get_current_firm)):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select matter_id, slug, title, area, side, jurisdiction, phase,
                   theory_md, opposing_party, client_name, active,
                   created_by_user_id, created_at, updated_at
              from matters_workspace
             where matter_id = $1::uuid and firm_id = $2 limit 1
            """,
            matter_id, str(principal.firm_id),
        )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "matter no encontrado")
    return {
        "matter_id": str(row["matter_id"]),
        "slug": row["slug"],
        "title": row["title"],
        "area": row["area"],
        "side": row["side"],
        "jurisdiction": row["jurisdiction"],
        "phase": row["phase"],
        "theory_md": row["theory_md"],
        "opposing_party": row["opposing_party"],
        "client_name": row["client_name"],
        "active": row["active"],
        "created_by_user_id": str(row["created_by_user_id"]) if row["created_by_user_id"] else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


# ─── PATCH · update ────────────────────────────────────────────

@router.patch("/{matter_id}")
async def update_matter(
    matter_id: str, body: MatterUpdateV2,
    principal: Principal = Depends(get_current_firm),
):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")

    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not updates:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "sin campos a actualizar")

    set_parts = []
    args: list = []
    for k, v in updates.items():
        args.append(v)
        set_parts.append(f"{k} = ${len(args)}")
    args.append(matter_id)
    args.append(str(principal.firm_id))
    sql = f"""
        update matters_workspace
           set {', '.join(set_parts)}, updated_at = now()
         where matter_id = ${len(args)-1}::uuid and firm_id = ${len(args)}
         returning matter_id
    """
    async with pool.acquire() as conn:
        updated = await conn.fetchval(sql, *args)
        if updated:
            # Append history
            try:
                import json as _json
                await conn.execute(
                    """
                    insert into matter_history
                        (matter_id, firm_id, event_type, actor_user_id,
                         actor_agent, summary, details)
                    values ($1::uuid, $2::uuid, 'matter_updated', $3, 'api', $4, $5::jsonb)
                    """,
                    matter_id, str(principal.firm_id),
                    str(principal.user_id) if principal.user_id else None,
                    f"Matter actualizado ({', '.join(updates.keys())})",
                    _json.dumps(updates, default=str, ensure_ascii=False),
                )
            except Exception as e:
                logger.debug("history append fallo: %s", e)
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "matter no encontrado")
    return {"matter_id": matter_id, "updated_fields": list(updates.keys())}


# ─── POST · archive ────────────────────────────────────────────

@router.post("/{matter_id}/archive")
async def archive_matter(matter_id: str, principal: Principal = Depends(get_current_firm)):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    async with pool.acquire() as conn:
        archived = await conn.fetchval(
            """
            update matters_workspace
               set active = false, updated_at = now()
             where matter_id = $1::uuid and firm_id = $2
             returning matter_id
            """,
            matter_id, str(principal.firm_id),
        )
        if archived:
            try:
                await conn.execute(
                    """
                    insert into matter_history
                        (matter_id, firm_id, event_type, actor_user_id,
                         actor_agent, summary, details)
                    values ($1::uuid, $2::uuid, 'matter_archived', $3, 'api', $4, '{}'::jsonb)
                    """,
                    matter_id, str(principal.firm_id),
                    str(principal.user_id) if principal.user_id else None,
                    "Matter archivado",
                )
            except Exception as e:
                logger.debug("history append archive fallo: %s", e)
    if not archived:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "matter no encontrado")
    return {"matter_id": matter_id, "active": False}


# ─── GET · history (audit trail) ───────────────────────────────

@router.get("/{matter_id}/history")
async def get_matter_history(
    matter_id: str,
    principal: Principal = Depends(get_current_firm),
    limit: int = Query(100, le=500),
):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")

    async with pool.acquire() as conn:
        # Guard: matter must belong to this firm (defense-in-depth ademas de RLS)
        owner = await conn.fetchval(
            "select 1 from matters_workspace where matter_id = $1::uuid and firm_id = $2 limit 1",
            matter_id, str(principal.firm_id),
        )
        if not owner:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "matter no encontrado")

        rows = await conn.fetch(
            """
            select event_id, event_type, actor_user_id, actor_agent,
                   summary, details, created_at
              from matter_history
             where matter_id = $1::uuid and firm_id = $2
             order by created_at desc, event_id desc
             limit $3
            """,
            matter_id, str(principal.firm_id), int(limit),
        )
    return {
        "matter_id": matter_id,
        "events": [
            {
                "event_id": str(r["event_id"]),
                "event_type": r["event_type"],
                "actor_user_id": str(r["actor_user_id"]) if r["actor_user_id"] else None,
                "actor_agent": r["actor_agent"],
                "summary": r["summary"],
                "details": dict(r["details"] or {}),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }
