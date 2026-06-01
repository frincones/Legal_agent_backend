"""Sprint M21.S7.C · Cookbooks API."""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm
from utils.db import get_storage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v2/cookbooks", tags=["cookbooks"])


@router.get("")
async def list_cookbooks(
    principal: Principal = Depends(get_current_firm),
    category: Optional[str] = None,
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
        select cookbook_id, name, category, short_description, long_description_md,
               icon, inputs_schema, estimated_minutes, pricing_tier,
               version, documentation_url
          from cookbook_registry
         {('where ' + ' and '.join(where)) if where else ''}
         order by category, name
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return {
        "items": [
            {
                "cookbook_id": r["cookbook_id"], "name": r["name"], "category": r["category"],
                "short_description": r["short_description"],
                "long_description_md": r["long_description_md"],
                "icon": r["icon"],
                "inputs_schema": dict(r["inputs_schema"] or {}),
                "estimated_minutes": r["estimated_minutes"],
                "pricing_tier": r["pricing_tier"],
                "version": r["version"],
                "documentation_url": r["documentation_url"],
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.get("/{cookbook_id}")
async def get_cookbook(cookbook_id: str, principal: Principal = Depends(get_current_firm)):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            select cookbook_id, name, category, short_description, long_description_md,
                   icon, inputs_schema, steps, estimated_minutes, pricing_tier,
                   version, documentation_url
              from cookbook_registry where cookbook_id=$1
            """,
            cookbook_id,
        )
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"cookbook {cookbook_id!r} no existe")
    return {
        "cookbook_id": r["cookbook_id"], "name": r["name"], "category": r["category"],
        "short_description": r["short_description"],
        "long_description_md": r["long_description_md"],
        "icon": r["icon"],
        "inputs_schema": dict(r["inputs_schema"] or {}),
        "steps": list(r["steps"] or []),
        "estimated_minutes": r["estimated_minutes"],
        "pricing_tier": r["pricing_tier"],
        "version": r["version"],
        "documentation_url": r["documentation_url"],
    }


class RunBody(BaseModel):
    inputs: dict = Field(default_factory=dict)
    matter_id: Optional[str] = None


@router.post("/{cookbook_id}/run")
async def run_cookbook(cookbook_id: str, body: RunBody, principal: Principal = Depends(get_current_firm)):
    from lex.cookbooks.executor import execute_cookbook
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    anthropic_client = getattr(storage, "anthropic_client", None)
    openai_client = getattr(storage, "openai_client", None)
    try:
        result = await execute_cookbook(
            cookbook_id=cookbook_id,
            firm_id=UUID(str(principal.firm_id)),
            user_id=UUID(str(principal.user_id)) if principal.user_id else None,
            inputs=body.inputs or {},
            pool=pool, anthropic_client=anthropic_client, openai_client=openai_client,
            matter_id=UUID(body.matter_id) if body.matter_id else None,
        )
        return result
    except Exception as e:
        logger.exception("run_cookbook failed")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


@router.get("/runs/list")
async def list_runs(
    principal: Principal = Depends(get_current_firm),
    limit: int = Query(50, le=200),
    cookbook_id: Optional[str] = None,
):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    where = ["firm_id = $1"]
    args: list = [str(principal.firm_id)]
    if cookbook_id:
        args.append(cookbook_id)
        where.append(f"cookbook_id = ${len(args)}")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            select run_id, cookbook_id, started_at, finished_at, duration_ms,
                   status, error_message
              from cookbook_runs
             where {' and '.join(where)}
             order by started_at desc
             limit {int(limit)}
            """,
            *args,
        )
    return {
        "items": [
            {
                "run_id": str(r["run_id"]), "cookbook_id": r["cookbook_id"],
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
                "duration_ms": r["duration_ms"],
                "status": r["status"], "error_message": r["error_message"],
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.get("/runs/{run_id}")
async def get_run(run_id: str, principal: Principal = Depends(get_current_firm)):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    async with pool.acquire() as conn:
        run = await conn.fetchrow(
            """
            select run_id, cookbook_id, matter_id, started_at, finished_at,
                   duration_ms, status, inputs, outputs, error_message
              from cookbook_runs where run_id=$1::uuid and firm_id=$2
            """,
            run_id, str(principal.firm_id),
        )
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "run no encontrado")
        steps = await conn.fetch(
            """
            select step_index, step_name, started_at, finished_at, duration_ms,
                   status, output, error_message
              from cookbook_step_logs
             where run_id=$1::uuid order by step_index
            """,
            run_id,
        )
    return {
        "run_id": str(run["run_id"]), "cookbook_id": run["cookbook_id"],
        "matter_id": str(run["matter_id"]) if run["matter_id"] else None,
        "started_at": run["started_at"].isoformat() if run["started_at"] else None,
        "finished_at": run["finished_at"].isoformat() if run["finished_at"] else None,
        "duration_ms": run["duration_ms"], "status": run["status"],
        "inputs": dict(run["inputs"] or {}),
        "outputs": dict(run["outputs"] or {}),
        "error_message": run["error_message"],
        "steps": [
            {
                "step_index": s["step_index"], "step_name": s["step_name"],
                "started_at": s["started_at"].isoformat() if s["started_at"] else None,
                "finished_at": s["finished_at"].isoformat() if s["finished_at"] else None,
                "duration_ms": s["duration_ms"], "status": s["status"],
                "output": dict(s["output"] or {}) if isinstance(s["output"], dict) else s["output"],
                "error_message": s["error_message"],
            }
            for s in steps
        ],
    }
