"""Sprint M21.S5.G · MCP Connectors Registry API.

Endpoints (firm-scoped):
  GET    /v2/connectors                       · catalog completo + subscripcion del firm
  GET    /v2/connectors/{connector_id}        · detalle + ultimos health checks
  POST   /v2/connectors/{connector_id}/subscribe  · subscribe firm a connector
  POST   /v2/connectors/{connector_id}/unsubscribe· revoke
  POST   /v2/connectors/{connector_id}/probe     · health probe (HEAD/GET base_url)
  GET    /v2/connectors/categories            · categorias agrupadas
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from utils.auth import Principal, get_current_firm
from utils.db import get_storage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v2/connectors", tags=["mcp-connectors"])


@router.get("")
async def list_connectors(
    principal: Principal = Depends(get_current_firm),
    category: Optional[str] = None,
):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    where = ["enabled = true"]
    args: list = []
    if category:
        args.append(category)
        where.append(f"category = ${len(args)}")
    sql_registry = f"""
        select connector_id, name, category, description, jurisdiction,
               base_url, api_kind, auth_required, rate_limit_rps, tags,
               documentation_url
          from mcp_connectors_registry
         where {' and '.join(where)}
         order by category, name
    """
    async with pool.acquire() as conn:
        registry_rows = await conn.fetch(sql_registry, *args)
        sub_rows = await conn.fetch(
            """
            select connector_id, enabled, last_used_at, use_count, config
              from firm_mcp_subscriptions
             where firm_id = $1
            """,
            str(principal.firm_id),
        )
    subs = {r["connector_id"]: r for r in sub_rows}
    items = []
    for r in registry_rows:
        sub = subs.get(r["connector_id"])
        items.append({
            "connector_id": r["connector_id"],
            "name": r["name"],
            "category": r["category"],
            "description": r["description"],
            "jurisdiction": r["jurisdiction"],
            "base_url": r["base_url"],
            "api_kind": r["api_kind"],
            "auth_required": r["auth_required"],
            "rate_limit_rps": r["rate_limit_rps"],
            "tags": list(r["tags"] or []),
            "documentation_url": r["documentation_url"],
            "subscription": None if sub is None else {
                "enabled": sub["enabled"],
                "last_used_at": sub["last_used_at"].isoformat() if sub["last_used_at"] else None,
                "use_count": sub["use_count"],
                "config": dict(sub["config"] or {}),
            },
        })
    return {"items": items, "total": len(items)}


@router.get("/categories")
async def categories(principal: Principal = Depends(get_current_firm)):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select category, count(*) as n
              from mcp_connectors_registry where enabled=true
             group by category order by category
            """,
        )
    return {"items": [{"category": r["category"], "count": int(r["n"])} for r in rows]}


@router.get("/{connector_id}")
async def get_connector(connector_id: str, principal: Principal = Depends(get_current_firm)):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    async with pool.acquire() as conn:
        reg = await conn.fetchrow(
            """
            select connector_id, name, category, description, jurisdiction,
                   base_url, api_kind, auth_required, rate_limit_rps, tags,
                   documentation_url
              from mcp_connectors_registry where connector_id=$1
            """,
            connector_id,
        )
        if reg is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"connector {connector_id!r} no existe")
        sub = await conn.fetchrow(
            "select * from firm_mcp_subscriptions where firm_id=$1 and connector_id=$2",
            str(principal.firm_id), connector_id,
        )
        health = await conn.fetch(
            """
            select checked_at, status, latency_ms, error_message
              from connector_health
             where connector_id=$1 order by checked_at desc limit 10
            """,
            connector_id,
        )
    return {
        "connector_id": reg["connector_id"],
        "name": reg["name"],
        "category": reg["category"],
        "description": reg["description"],
        "jurisdiction": reg["jurisdiction"],
        "base_url": reg["base_url"],
        "api_kind": reg["api_kind"],
        "auth_required": reg["auth_required"],
        "rate_limit_rps": reg["rate_limit_rps"],
        "tags": list(reg["tags"] or []),
        "documentation_url": reg["documentation_url"],
        "subscription": None if sub is None else {
            "enabled": sub["enabled"],
            "last_used_at": sub["last_used_at"].isoformat() if sub["last_used_at"] else None,
            "use_count": sub["use_count"],
            "config": dict(sub["config"] or {}),
        },
        "recent_health": [
            {
                "checked_at": r["checked_at"].isoformat() if r["checked_at"] else None,
                "status": r["status"],
                "latency_ms": r["latency_ms"],
                "error_message": r["error_message"],
            }
            for r in health
        ],
    }


class SubscribeBody(BaseModel):
    config: dict = {}


@router.post("/{connector_id}/subscribe")
async def subscribe(connector_id: str, body: SubscribeBody = SubscribeBody(), principal: Principal = Depends(get_current_firm)):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    import json as _json
    async with pool.acquire() as conn:
        exists = await conn.fetchval("select 1 from mcp_connectors_registry where connector_id=$1", connector_id)
        if not exists:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"connector {connector_id!r} no existe")
        await conn.execute(
            """
            insert into firm_mcp_subscriptions (firm_id, connector_id, enabled, config)
            values ($1, $2, true, $3::jsonb)
            on conflict (firm_id, connector_id) do update set
                enabled = true,
                config = excluded.config
            """,
            str(principal.firm_id), connector_id,
            _json.dumps(body.config or {}, ensure_ascii=False),
        )
    return {"connector_id": connector_id, "subscribed": True}


@router.post("/{connector_id}/unsubscribe")
async def unsubscribe(connector_id: str, principal: Principal = Depends(get_current_firm)):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    async with pool.acquire() as conn:
        await conn.execute(
            """
            update firm_mcp_subscriptions set enabled=false
             where firm_id=$1 and connector_id=$2
            """,
            str(principal.firm_id), connector_id,
        )
    return {"connector_id": connector_id, "subscribed": False}


@router.post("/{connector_id}/probe")
async def probe(connector_id: str, principal: Principal = Depends(get_current_firm)):
    """Health probe: hace HEAD/GET al base_url y persiste resultado."""
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    async with pool.acquire() as conn:
        reg = await conn.fetchrow("select base_url from mcp_connectors_registry where connector_id=$1", connector_id)
    if reg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"connector {connector_id!r} no existe")
    url = reg["base_url"]
    if not url:
        return {"connector_id": connector_id, "status": "no_base_url"}

    start = time.time()
    status_label = "down"
    err: Optional[str] = None
    latency_ms: Optional[int] = None
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True, verify=False) as client:
            resp = await client.head(url)
            if resp.status_code in (405, 501):
                resp = await client.get(url)
            latency_ms = int((time.time() - start) * 1000)
            if 200 <= resp.status_code < 400:
                status_label = "up"
            elif resp.status_code >= 500:
                status_label = "down"
                err = f"HTTP {resp.status_code}"
            else:
                status_label = "degraded"
                err = f"HTTP {resp.status_code}"
    except Exception as e:
        latency_ms = int((time.time() - start) * 1000)
        status_label = "down"
        err = f"{type(e).__name__}: {e}"

    async with pool.acquire() as conn:
        try:
            await conn.execute(
                """
                insert into connector_health (connector_id, status, latency_ms, error_message)
                values ($1, $2, $3, $4)
                """,
                connector_id, status_label, latency_ms, err and err[:500],
            )
        except Exception as e:
            logger.debug("probe insert failed: %s", e)
    return {
        "connector_id": connector_id,
        "status": status_label,
        "latency_ms": latency_ms,
        "error": err,
    }
