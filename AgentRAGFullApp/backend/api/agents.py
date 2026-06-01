"""Sprint M21.S4.F · Background Agents API.

Endpoints (firm-scoped):
  GET    /v2/agents                          · catalog + jobs config del firm
  GET    /v2/agents/{name}                   · detalle de un agent + ultima ejecucion
  POST   /v2/agents/{name}/run               · ejecuta on-demand (manual)
  POST   /v2/agents/{name}/toggle            · enable/disable
  GET    /v2/agents/{name}/logs              · ultimos N agent_run_logs
  GET    /v2/agents/runs                     · all runs del firm (cross-agent)
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
router = APIRouter(prefix="/v2/agents", tags=["agents"])


@router.get("")
async def list_agents(principal: Principal = Depends(get_current_firm)):
    from lex.agents.registry import get_agent_registry
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    registry = get_agent_registry()
    agents = [
        {
            "name": a.name,
            "description": a.description,
            "trigger_kind": a.trigger_kind,
            "default_cron": a.default_cron,
            "default_event": a.default_event,
            "timeout_seconds": a.timeout_seconds,
        }
        for a in registry.list()
    ]
    jobs_by_name = {}
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    select agent_name, job_id, enabled, schedule_cron, config,
                           last_run_at, last_run_status
                      from firm_background_jobs
                     where firm_id = $1
                    """,
                    str(principal.firm_id),
                )
            for r in rows:
                jobs_by_name[r["agent_name"]] = {
                    "job_id": str(r["job_id"]),
                    "enabled": r["enabled"],
                    "schedule_cron": r["schedule_cron"],
                    "config": dict(r["config"] or {}),
                    "last_run_at": r["last_run_at"].isoformat() if r["last_run_at"] else None,
                    "last_run_status": r["last_run_status"],
                }
        except Exception as e:
            logger.warning("list_agents jobs query failed: %s", e)
    items = []
    for a in agents:
        items.append({**a, "job": jobs_by_name.get(a["name"])})
    return {"items": items, "total": len(items)}


@router.get("/runs")
async def list_runs(
    principal: Principal = Depends(get_current_firm),
    limit: int = Query(50, le=200),
    agent_name: Optional[str] = None,
):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    where = ["firm_id = $1"]
    args: list = [str(principal.firm_id)]
    if agent_name:
        args.append(agent_name)
        where.append(f"agent_name = ${len(args)}")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            select run_id, agent_name, trigger_kind, started_at, finished_at,
                   duration_ms, status, items_processed, items_succeeded,
                   items_failed, output_summary, error_message
              from agent_run_logs
             where {' and '.join(where)}
             order by started_at desc
             limit {int(limit)}
            """,
            *args,
        )
    return {
        "items": [
            {
                "run_id": str(r["run_id"]),
                "agent_name": r["agent_name"],
                "trigger_kind": r["trigger_kind"],
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
                "duration_ms": r["duration_ms"],
                "status": r["status"],
                "items_processed": r["items_processed"],
                "items_succeeded": r["items_succeeded"],
                "items_failed": r["items_failed"],
                "output_summary": r["output_summary"],
                "error_message": r["error_message"],
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.get("/{name}")
async def get_agent(name: str, principal: Principal = Depends(get_current_firm)):
    from lex.agents.registry import get_agent_registry
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    registry = get_agent_registry()
    agent = registry.get(name)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"agent {name!r} no existe")
    job = None
    last_runs = []
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    select job_id, enabled, schedule_cron, config,
                           last_run_at, last_run_status
                      from firm_background_jobs
                     where firm_id = $1 and agent_name = $2
                    """,
                    str(principal.firm_id), name,
                )
                if row:
                    job = {
                        "job_id": str(row["job_id"]), "enabled": row["enabled"],
                        "schedule_cron": row["schedule_cron"],
                        "config": dict(row["config"] or {}),
                        "last_run_at": row["last_run_at"].isoformat() if row["last_run_at"] else None,
                        "last_run_status": row["last_run_status"],
                    }
                rs = await conn.fetch(
                    """
                    select run_id, started_at, finished_at, duration_ms, status,
                           items_processed, items_succeeded, items_failed, output_summary
                      from agent_run_logs
                     where firm_id = $1 and agent_name = $2
                     order by started_at desc limit 10
                    """,
                    str(principal.firm_id), name,
                )
                last_runs = [
                    {
                        "run_id": str(r["run_id"]),
                        "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                        "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
                        "duration_ms": r["duration_ms"], "status": r["status"],
                        "items_processed": r["items_processed"],
                        "items_succeeded": r["items_succeeded"],
                        "items_failed": r["items_failed"],
                        "output_summary": r["output_summary"],
                    }
                    for r in rs
                ]
        except Exception as e:
            logger.warning("get_agent query failed: %s", e)
    return {
        "name": agent.name,
        "description": agent.description,
        "trigger_kind": agent.trigger_kind,
        "default_cron": agent.default_cron,
        "default_event": agent.default_event,
        "timeout_seconds": agent.timeout_seconds,
        "job": job,
        "last_runs": last_runs,
    }


class RunBody(BaseModel):
    config: dict = Field(default_factory=dict)


@router.post("/{name}/run")
async def run_agent(name: str, body: RunBody = RunBody(), principal: Principal = Depends(get_current_firm)):
    from lex.agents.dispatcher import dispatch_agent
    from lex.agents.registry import get_agent_registry

    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    registry = get_agent_registry()
    if registry.get(name) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"agent {name!r} no existe")
    try:
        ant_client = getattr(storage, "anthropic_client", None) if storage else None
    except Exception:
        ant_client = None
    result = await dispatch_agent(
        name=name,
        firm_id=UUID(str(principal.firm_id)),
        pool=pool, anthropic_client=ant_client,
        trigger_kind="manual",
        config=body.config or {},
    )
    return {
        "agent_name": name,
        "status": result.status,
        "items_processed": result.items_processed,
        "items_succeeded": result.items_succeeded,
        "items_failed": result.items_failed,
        "output_summary": result.output_summary,
        "error_message": result.error_message,
    }


class ToggleBody(BaseModel):
    enabled: bool


@router.post("/{name}/toggle")
async def toggle_agent(name: str, body: ToggleBody, principal: Principal = Depends(get_current_firm)):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    async with pool.acquire() as conn:
        await conn.execute(
            """
            insert into firm_background_jobs (firm_id, agent_name, enabled)
            values ($1, $2, $3)
            on conflict (firm_id, agent_name) do update
              set enabled = excluded.enabled, updated_at = now()
            """,
            str(principal.firm_id), name, body.enabled,
        )
    return {"agent_name": name, "enabled": body.enabled}


@router.get("/{name}/logs")
async def agent_logs(
    name: str,
    principal: Principal = Depends(get_current_firm),
    limit: int = Query(50, le=200),
):
    storage = await get_storage()
    pool = getattr(storage, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage pool unavailable")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select run_id, started_at, finished_at, duration_ms, status,
                   trigger_kind, items_processed, items_succeeded, items_failed,
                   output_summary, error_message, metadata
              from agent_run_logs
             where firm_id = $1 and agent_name = $2
             order by started_at desc limit $3
            """,
            str(principal.firm_id), name, int(limit),
        )
    return {
        "agent_name": name,
        "items": [
            {
                "run_id": str(r["run_id"]),
                "trigger_kind": r["trigger_kind"],
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
                "duration_ms": r["duration_ms"],
                "status": r["status"],
                "items_processed": r["items_processed"],
                "items_succeeded": r["items_succeeded"],
                "items_failed": r["items_failed"],
                "output_summary": r["output_summary"],
                "error_message": r["error_message"],
                "metadata": dict(r["metadata"] or {}),
            }
            for r in rows
        ],
        "total": len(rows),
    }
