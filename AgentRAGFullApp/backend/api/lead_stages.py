"""Sprint 9 · Lead stages CRUD."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/lead-stages", tags=["lead_stages"])


VALID_COLORS = {"blue", "green", "amber", "red", "purple", "gray"}


@router.get("")
async def list_stages(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, name, sort_order, color, is_won, is_lost
              from lead_stages where firm_id = $1::uuid
             order by sort_order asc
            """,
            principal.firm_id,
        )
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]),
                "name": r["name"],
                "sort_order": r["sort_order"],
                "color": r["color"],
                "is_won": r["is_won"],
                "is_lost": r["is_lost"],
            }
            for r in rows
        ],
    }


class CreateRequest(BaseModel):
    name: str = Field(min_length=2)
    color: str = Field(default="blue")
    sort_order: Optional[int] = None
    is_won: bool = False
    is_lost: bool = False


@router.post("")
async def create_stage(
    body: CreateRequest,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior", "socio_junior"):
        raise HTTPException(403, "Solo socios/admin")
    if body.color not in VALID_COLORS:
        raise HTTPException(400, f"color inválido (válidos: {sorted(VALID_COLORS)})")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        sort = body.sort_order
        if sort is None:
            sort = (await conn.fetchval(
                "select coalesce(max(sort_order), 0) + 10 from lead_stages where firm_id = $1::uuid",
                principal.firm_id,
            ))
        row = await conn.fetchrow(
            """
            insert into lead_stages (firm_id, name, sort_order, color, is_won, is_lost)
            values ($1::uuid, $2, $3, $4, $5, $6)
            on conflict (firm_id, name) do nothing
            returning id, name, sort_order, color, is_won, is_lost
            """,
            principal.firm_id, body.name, sort, body.color, body.is_won, body.is_lost,
        )
    if not row:
        raise HTTPException(409, "ya existe un stage con ese nombre")
    return {
        "id": str(row["id"]), "name": row["name"], "sort_order": row["sort_order"],
        "color": row["color"], "is_won": row["is_won"], "is_lost": row["is_lost"],
    }


class PatchRequest(BaseModel):
    name: Optional[str] = None
    sort_order: Optional[int] = None
    color: Optional[str] = None
    is_won: Optional[bool] = None
    is_lost: Optional[bool] = None


@router.patch("/{stage_id}")
async def patch_stage(
    stage_id: str,
    body: PatchRequest,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior", "socio_junior"):
        raise HTTPException(403, "Solo socios/admin")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    fields, params = [], [stage_id, principal.firm_id]
    for f in ("name", "sort_order", "color", "is_won", "is_lost"):
        v = getattr(body, f)
        if v is not None:
            if f == "color" and v not in VALID_COLORS:
                raise HTTPException(400, "color inválido")
            params.append(v); fields.append(f"{f} = ${len(params)}")
    if not fields:
        raise HTTPException(400, "nada que actualizar")
    sql = f"""
        update lead_stages set {', '.join(fields)}
         where id = $1::uuid and firm_id = $2::uuid
         returning id, name, sort_order, color, is_won, is_lost
    """
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(sql, *params)
    if not row:
        raise HTTPException(404, "not found")
    return {
        "id": str(row["id"]), "name": row["name"], "sort_order": row["sort_order"],
        "color": row["color"], "is_won": row["is_won"], "is_lost": row["is_lost"],
    }


class ReorderRequest(BaseModel):
    order: list[str]                                        # lista de stage_ids en el orden deseado


@router.post("/reorder")
async def reorder(
    body: ReorderRequest,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior", "socio_junior"):
        raise HTTPException(403, "Solo socios/admin")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        for i, sid in enumerate(body.order):
            await conn.execute(
                """update lead_stages set sort_order = $3
                    where id = $1::uuid and firm_id = $2::uuid""",
                sid, principal.firm_id, (i + 1) * 10,
            )
    return {"ok": True}


@router.delete("/{stage_id}")
async def delete_stage(
    stage_id: str,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        in_use = await conn.fetchval(
            "select count(*) from leads where stage_id = $1::uuid",
            stage_id,
        )
        if (in_use or 0) > 0:
            raise HTTPException(409, f"hay {in_use} leads en esta etapa; muévelos primero")
        await conn.execute(
            "delete from lead_stages where id = $1::uuid and firm_id = $2::uuid",
            stage_id, principal.firm_id,
        )
    return {"deleted": True}
