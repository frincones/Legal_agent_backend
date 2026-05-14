"""Sprint 14 · API keys admin API (auth Supabase, no API key).

  GET    /v1/api-keys              · lista
  POST   /v1/api-keys              · crea (devuelve plain UNA SOLA VEZ)
  PATCH  /v1/api-keys/{id}         · update (nombre, scopes, rate_limit, active)
  POST   /v1/api-keys/{id}/revoke
  DELETE /v1/api-keys/{id}
  GET    /v1/api-keys/{id}/usage   · log de uso

Solo accesible para admin/socio_senior.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm
from utils.api_keys import generate_key, validate_scopes, VALID_SCOPES

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/api-keys", tags=["api_keys"])


@router.get("")
async def list_keys(principal: Principal = Depends(get_current_firm)):
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin / socio_senior")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, name, prefix, scopes, rate_limit_per_min, active,
                   expires_at, last_used_at, last_used_ip::text as last_used_ip,
                   use_count, revoked_at, created_at
              from api_keys
             where firm_id = $1::uuid
             order by created_at desc
            """,
            principal.firm_id,
        )
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]),
                "name": r["name"],
                "prefix": r["prefix"],
                "scopes": list(r["scopes"] or []),
                "rate_limit_per_min": r["rate_limit_per_min"],
                "active": r["active"],
                "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
                "last_used_at": r["last_used_at"].isoformat() if r["last_used_at"] else None,
                "last_used_ip": r["last_used_ip"],
                "use_count": r["use_count"],
                "revoked_at": r["revoked_at"].isoformat() if r["revoked_at"] else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
        "valid_scopes": sorted(VALID_SCOPES),
    }


class CreateRequest(BaseModel):
    name: str = Field(min_length=2)
    scopes: list[str] = Field(default_factory=lambda: ["read"])
    rate_limit_per_min: int = Field(default=60, ge=1, le=10000)
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=3650)


@router.post("")
async def create_key(
    body: CreateRequest,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin / socio_senior")
    ok, invalid = validate_scopes(body.scopes)
    if not ok:
        raise HTTPException(400, f"scopes inválidos: {invalid}")
    plain, prefix, hashed = generate_key()
    expires_at = None
    if body.expires_in_days:
        expires_at = datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into api_keys
              (firm_id, name, prefix, key_hash, scopes, rate_limit_per_min,
               expires_at, created_by)
            values ($1::uuid, $2, $3, $4, $5::text[], $6, $7::timestamptz, $8::uuid)
            returning id, prefix, scopes, rate_limit_per_min, expires_at, created_at
            """,
            principal.firm_id, body.name, prefix, hashed,
            body.scopes, body.rate_limit_per_min, expires_at, principal.user_id,
        )
    return {
        "id": str(row["id"]),
        "name": body.name,
        "prefix": row["prefix"],
        "scopes": list(row["scopes"]),
        "rate_limit_per_min": row["rate_limit_per_min"],
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        "created_at": row["created_at"].isoformat(),
        "plain_key": plain,
        "warning": "Esta key NO se mostrará de nuevo. Cópiala ahora.",
    }


class PatchRequest(BaseModel):
    name: Optional[str] = None
    scopes: Optional[list[str]] = None
    rate_limit_per_min: Optional[int] = Field(default=None, ge=1, le=10000)
    active: Optional[bool] = None


@router.patch("/{key_id}")
async def patch_key(
    key_id: str,
    body: PatchRequest,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin / socio_senior")
    if body.scopes is not None:
        ok, invalid = validate_scopes(body.scopes)
        if not ok:
            raise HTTPException(400, f"scopes inválidos: {invalid}")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    fields, params = [], [key_id, principal.firm_id]
    if body.name is not None:
        params.append(body.name); fields.append(f"name = ${len(params)}")
    if body.scopes is not None:
        params.append(body.scopes); fields.append(f"scopes = ${len(params)}::text[]")
    if body.rate_limit_per_min is not None:
        params.append(body.rate_limit_per_min); fields.append(f"rate_limit_per_min = ${len(params)}")
    if body.active is not None:
        params.append(body.active); fields.append(f"active = ${len(params)}")
    if not fields:
        raise HTTPException(400, "nada que actualizar")
    sql = f"""
        update api_keys set {', '.join(fields)}
         where id = $1::uuid and firm_id = $2::uuid
         returning id, name, prefix, scopes, rate_limit_per_min, active
    """
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(sql, *params)
    if not row:
        raise HTTPException(404, "not found")
    return {
        "id": str(row["id"]), "name": row["name"], "prefix": row["prefix"],
        "scopes": list(row["scopes"]), "rate_limit_per_min": row["rate_limit_per_min"],
        "active": row["active"],
    }


@router.post("/{key_id}/revoke")
async def revoke(
    key_id: str,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "update api_keys set revoked_at = now(), active = false where id = $1::uuid and firm_id = $2::uuid",
            key_id, principal.firm_id,
        )
    return {"revoked": True}


@router.delete("/{key_id}")
async def delete_key(
    key_id: str,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "delete from api_keys where id = $1::uuid and firm_id = $2::uuid",
            key_id, principal.firm_id,
        )
    return {"deleted": True}


@router.get("/{key_id}/usage")
async def usage(
    key_id: str,
    limit: int = Query(default=100, le=500),
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior", "socio_junior"):
        raise HTTPException(403, "Solo socios/admin")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select endpoint, method, status_code, duration_ms,
                   ip_address::text as ip_address, user_agent, occurred_at
              from api_key_usage_log
             where api_key_id = $1::uuid and firm_id = $2::uuid
             order by occurred_at desc
             limit $3
            """,
            key_id, principal.firm_id, limit,
        )
    return {
        "count": len(rows),
        "items": [
            {
                "endpoint": r["endpoint"], "method": r["method"],
                "status_code": r["status_code"], "duration_ms": r["duration_ms"],
                "ip_address": r["ip_address"], "user_agent": r["user_agent"],
                "occurred_at": r["occurred_at"].isoformat() if r["occurred_at"] else None,
            }
            for r in rows
        ],
    }
