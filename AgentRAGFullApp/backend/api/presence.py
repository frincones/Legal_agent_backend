"""Sprint 16 · Presence API.

  POST /v1/presence/heartbeat { matter_id, location_kind?, location_ref? }
       → UPSERT (user, matter, location_kind, location_ref) con last_heartbeat=now()

  GET  /v1/presence/active?matter_id=...&window=90
       → usuarios activos en los últimos N segundos vía RPC lexai_active_users

  POST /v1/presence/leave   { matter_id?, location_kind?, location_ref? }
       → borra la fila (ej. al cerrar pestaña/usuario sale del matter)

Ligero a propósito: no usamos WebSockets · el frontend hace heartbeat 30s.
TTL filtramos en query (no hace falta cron de limpieza, pero podemos
añadir una vacuum job más adelante).
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/presence", tags=["presence"])


VALID_LOCATIONS = {"matter", "matter_document", "canvas", "dashboard", "other"}


class HeartbeatIn(BaseModel):
    matter_id: Optional[str] = None
    location_kind: str = "matter"
    location_ref: Optional[str] = None


class LeaveIn(BaseModel):
    matter_id: Optional[str] = None
    location_kind: Optional[str] = None
    location_ref: Optional[str] = None


@router.post("/heartbeat")
async def heartbeat(
    body: HeartbeatIn,
    principal: Principal = Depends(get_current_firm),
):
    if body.location_kind not in VALID_LOCATIONS:
        raise HTTPException(400, f"location_kind inválido (válidos: {sorted(VALID_LOCATIONS)})")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"ok": False, "status": "no_storage"}
    async with storage.pool.acquire() as conn:
        # location_ref puede ser '' o null · normalizamos a '' para que la unique key matchee.
        loc_ref = body.location_ref or ""
        await conn.execute(
            """
            insert into presence_sessions
              (firm_id, user_id, matter_id, location_kind, location_ref,
               started_at, last_heartbeat)
            values ($1::uuid, $2::uuid, $3::uuid, $4, $5, now(), now())
            on conflict (user_id, matter_id, location_kind, location_ref)
            do update set last_heartbeat = now()
            """,
            principal.firm_id, principal.user_id, body.matter_id,
            body.location_kind, loc_ref,
        )
    return {"ok": True}


@router.get("/active")
async def active(
    matter_id: str = Query(..., min_length=1),
    window: int = Query(default=90, ge=10, le=600),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select * from lexai_active_users($1::uuid, $2::uuid, $3)
            """,
            principal.firm_id, matter_id, window,
        )
    return {
        "items": [
            {
                "user_id": str(r["user_id"]),
                "full_name": r["full_name"],
                "avatar_url": r["avatar_url"],
                "location_kind": r["location_kind"],
                "location_ref": r["location_ref"],
                "last_heartbeat": r["last_heartbeat"].isoformat() if r["last_heartbeat"] else None,
                "is_self": str(r["user_id"]) == str(principal.user_id),
            }
            for r in rows
        ]
    }


@router.post("/leave")
async def leave(
    body: LeaveIn,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"ok": False}
    async with storage.pool.acquire() as conn:
        if body.matter_id and body.location_kind:
            await conn.execute(
                """
                delete from presence_sessions
                 where firm_id = $1::uuid and user_id = $2::uuid
                   and matter_id = $3::uuid
                   and location_kind = $4
                   and location_ref = $5
                """,
                principal.firm_id, principal.user_id, body.matter_id,
                body.location_kind, body.location_ref or "",
            )
        elif body.matter_id:
            await conn.execute(
                """
                delete from presence_sessions
                 where firm_id = $1::uuid and user_id = $2::uuid
                   and matter_id = $3::uuid
                """,
                principal.firm_id, principal.user_id, body.matter_id,
            )
        else:
            # leave all
            await conn.execute(
                "delete from presence_sessions where firm_id = $1::uuid and user_id = $2::uuid",
                principal.firm_id, principal.user_id,
            )
    return {"ok": True}
