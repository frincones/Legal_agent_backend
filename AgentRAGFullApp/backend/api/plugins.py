"""Sprint M21.S6.B · Plugin Marketplace API."""
from __future__ import annotations

import json
import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from utils.auth import Principal, get_current_firm
from utils.db import get_storage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v2/plugins", tags=["plugins"])


@router.get("")
async def list_plugins(
    principal: Principal = Depends(get_current_firm),
    category: Optional[str] = None,
    installed_only: bool = Query(False),
):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    where = []
    args: list = []
    if category:
        args.append(category)
        where.append(f"category = ${len(args)}")
    sql = f"""
        select plugin_id, name, category, short_description, long_description_md,
               icon, route_path, api_namespaces, requires_modules,
               pricing_tier, version, documentation_url, screenshots
          from plugin_registry
         {('where ' + ' and '.join(where)) if where else ''}
         order by category, name
    """
    async with pool.acquire() as conn:
        registry = await conn.fetch(sql, *args)
        inst = await conn.fetch(
            "select plugin_id, enabled, installed_at, config from firm_plugin_installations where firm_id=$1",
            str(principal.firm_id),
        )
    inst_map = {i["plugin_id"]: i for i in inst}
    items = []
    for r in registry:
        i = inst_map.get(r["plugin_id"])
        installation = None
        if i is not None:
            installation = {
                "enabled": i["enabled"],
                "installed_at": i["installed_at"].isoformat() if i["installed_at"] else None,
                "config": dict(i["config"] or {}),
            }
        if installed_only and (installation is None or not installation["enabled"]):
            continue
        items.append({
            "plugin_id": r["plugin_id"], "name": r["name"], "category": r["category"],
            "short_description": r["short_description"],
            "long_description_md": r["long_description_md"],
            "icon": r["icon"], "route_path": r["route_path"],
            "api_namespaces": list(r["api_namespaces"] or []),
            "requires_modules": list(r["requires_modules"] or []),
            "pricing_tier": r["pricing_tier"], "version": r["version"],
            "documentation_url": r["documentation_url"],
            "screenshots": list(r["screenshots"] or []),
            "installation": installation,
        })
    return {"items": items, "total": len(items)}


@router.get("/{plugin_id}")
async def get_plugin(plugin_id: str, principal: Principal = Depends(get_current_firm)):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            select plugin_id, name, category, short_description, long_description_md,
                   icon, route_path, api_namespaces, requires_modules,
                   pricing_tier, version, documentation_url, screenshots
              from plugin_registry where plugin_id=$1
            """,
            plugin_id,
        )
        if r is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"plugin {plugin_id!r} no existe")
        i = await conn.fetchrow(
            "select enabled, installed_at, config from firm_plugin_installations where firm_id=$1 and plugin_id=$2",
            str(principal.firm_id), plugin_id,
        )
    return {
        "plugin_id": r["plugin_id"], "name": r["name"], "category": r["category"],
        "short_description": r["short_description"],
        "long_description_md": r["long_description_md"],
        "icon": r["icon"], "route_path": r["route_path"],
        "api_namespaces": list(r["api_namespaces"] or []),
        "requires_modules": list(r["requires_modules"] or []),
        "pricing_tier": r["pricing_tier"], "version": r["version"],
        "documentation_url": r["documentation_url"],
        "screenshots": list(r["screenshots"] or []),
        "installation": None if i is None else {
            "enabled": i["enabled"],
            "installed_at": i["installed_at"].isoformat() if i["installed_at"] else None,
            "config": dict(i["config"] or {}),
        },
    }


class InstallBody(BaseModel):
    config: dict = {}


@router.post("/{plugin_id}/install")
async def install(plugin_id: str, body: InstallBody = InstallBody(), principal: Principal = Depends(get_current_firm)):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    async with pool.acquire() as conn:
        exists = await conn.fetchval("select 1 from plugin_registry where plugin_id=$1", plugin_id)
        if not exists:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"plugin {plugin_id!r} no existe")
        await conn.execute(
            """
            insert into firm_plugin_installations (firm_id, plugin_id, enabled, installed_by_user_id, config)
            values ($1, $2, true, $3, $4::jsonb)
            on conflict (firm_id, plugin_id) do update set
                enabled = true, config = excluded.config
            """,
            str(principal.firm_id), plugin_id,
            str(principal.user_id) if principal.user_id else None,
            json.dumps(body.config or {}, ensure_ascii=False),
        )
    return {"plugin_id": plugin_id, "installed": True}


@router.post("/{plugin_id}/uninstall")
async def uninstall(plugin_id: str, principal: Principal = Depends(get_current_firm)):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    async with pool.acquire() as conn:
        await conn.execute(
            """
            update firm_plugin_installations set enabled=false
             where firm_id=$1 and plugin_id=$2
            """,
            str(principal.firm_id), plugin_id,
        )
    return {"plugin_id": plugin_id, "installed": False}


class ConfigBody(BaseModel):
    config: dict


@router.patch("/{plugin_id}/config")
async def update_config(plugin_id: str, body: ConfigBody, principal: Principal = Depends(get_current_firm)):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            """
            update firm_plugin_installations set config=$1::jsonb
             where firm_id=$2 and plugin_id=$3
             returning 1
            """,
            json.dumps(body.config, ensure_ascii=False),
            str(principal.firm_id), plugin_id,
        )
    if not n:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plugin no instalado")
    return {"plugin_id": plugin_id, "config_updated": True}


@router.get("/categories/list")
async def categories_list(principal: Principal = Depends(get_current_firm)):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    async with pool.acquire() as conn:
        rows = await conn.fetch("select category, count(*) as n from plugin_registry group by category order by category")
    return {"items": [{"category": r["category"], "count": int(r["n"])} for r in rows]}
