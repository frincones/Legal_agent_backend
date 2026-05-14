"""Sprint 17 · Saved filters API · vistas guardadas por usuario.

  GET    /v1/saved-filters?scope=matters|activity|tasks|documents|kb
  POST   /v1/saved-filters
  PATCH  /v1/saved-filters/{id}
  DELETE /v1/saved-filters/{id}

Multi-tenant: cada user_id sólo ve sus propias vistas.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/saved-filters", tags=["saved_filters"])


VALID_SCOPES = {"matters", "activity", "tasks", "documents", "kb"}


class FilterIn(BaseModel):
    scope: str
    name: str = Field(..., min_length=1, max_length=80)
    filters: dict = Field(default_factory=dict)
    pinned: bool = False
    sort_order: int = 0


class FilterPatch(BaseModel):
    name: Optional[str] = None
    filters: Optional[dict] = None
    pinned: Optional[bool] = None
    sort_order: Optional[int] = None


def _serialize(r) -> dict:
    return {
        "id": str(r["id"]),
        "scope": r["scope"],
        "name": r["name"],
        "filters": r["filters"] if not isinstance(r["filters"], str) else (json.loads(r["filters"]) if r["filters"] else {}),
        "pinned": bool(r["pinned"]),
        "sort_order": r["sort_order"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


@router.get("")
async def list_filters(
    scope: Optional[str] = Query(default=None),
    principal: Principal = Depends(get_current_firm),
):
    if scope and scope not in VALID_SCOPES:
        raise HTTPException(400, f"scope inválido (válidos: {sorted(VALID_SCOPES)})")
    where = ["firm_id = $1::uuid", "user_id = $2::uuid"]
    args: list = [principal.firm_id, principal.user_id]
    idx = 3
    if scope:
        where.append(f"scope = ${idx}"); args.append(scope); idx += 1
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            select id, scope, name, filters, pinned, sort_order, created_at, updated_at
              from saved_filters
             where {' and '.join(where)}
             order by pinned desc, sort_order, name
            """,
            *args,
        )
    return {"items": [_serialize(r) for r in rows]}


@router.post("", status_code=201)
async def create_filter(
    body: FilterIn,
    principal: Principal = Depends(get_current_firm),
):
    if body.scope not in VALID_SCOPES:
        raise HTTPException(400, f"scope inválido (válidos: {sorted(VALID_SCOPES)})")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                insert into saved_filters
                  (firm_id, user_id, scope, name, filters, pinned, sort_order)
                values ($1::uuid, $2::uuid, $3, $4, $5::jsonb, $6, $7)
                returning id, scope, name, filters, pinned, sort_order, created_at, updated_at
                """,
                principal.firm_id, principal.user_id, body.scope, body.name,
                json.dumps(body.filters or {}), body.pinned, body.sort_order,
            )
        except Exception as e:
            msg = str(e).lower()
            if "unique" in msg or "duplicate" in msg:
                raise HTTPException(409, "Ya tienes una vista con ese nombre en este scope")
            raise HTTPException(400, f"No se pudo crear: {e}")
    return _serialize(row)


@router.patch("/{filter_id}")
async def update_filter(
    filter_id: str,
    body: FilterPatch,
    principal: Principal = Depends(get_current_firm),
):
    sets: list[str] = []
    args: list = []
    idx = 1
    if body.name is not None:
        sets.append(f"name = ${idx}"); args.append(body.name); idx += 1
    if body.filters is not None:
        sets.append(f"filters = ${idx}::jsonb"); args.append(json.dumps(body.filters)); idx += 1
    if body.pinned is not None:
        sets.append(f"pinned = ${idx}"); args.append(body.pinned); idx += 1
    if body.sort_order is not None:
        sets.append(f"sort_order = ${idx}"); args.append(body.sort_order); idx += 1
    if not sets:
        raise HTTPException(400, "Sin cambios")
    args.append(principal.firm_id)
    args.append(principal.user_id)
    args.append(filter_id)
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            update saved_filters set {', '.join(sets)}
             where firm_id = ${idx}::uuid and user_id = ${idx + 1}::uuid and id = ${idx + 2}::uuid
             returning id, scope, name, filters, pinned, sort_order, created_at, updated_at
            """,
            *args,
        )
    if not row:
        raise HTTPException(404, "Filtro no encontrado")
    return _serialize(row)


@router.delete("/{filter_id}")
async def delete_filter(
    filter_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "delete from saved_filters where firm_id = $1::uuid and user_id = $2::uuid and id = $3::uuid",
            principal.firm_id, principal.user_id, filter_id,
        )
    return {"deleted": True}
