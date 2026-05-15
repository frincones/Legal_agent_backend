"""Sprint 28 · Status page público + worker de probes.

Endpoints:
  GET  /v1/public/status               · snapshot completo (componentes + incidents)
  GET  /v1/public/status/incidents     · solo incidents
  POST /v1/admin/status/probe-now      · admin · corre probes manualmente
  POST /v1/admin/status/components/{key}/status · set component status manualmente
  POST /v1/admin/status/incidents      · crear incident manual
  PATCH /v1/admin/status/incidents/{id} · update con timeline entry
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from utils.admin_guard import (
    AdminPrincipal, require_saas_admin, require_saas_admin_role, audit_admin_action,
)

logger = logging.getLogger(__name__)
public_router = APIRouter(prefix="/v1/public/status", tags=["status-public"])
admin_router = APIRouter(prefix="/v1/admin/status", tags=["status-admin"])


@public_router.get("")
async def status_summary():
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"overall_status": "degraded", "components": [], "recent_incidents": []}
    async with storage.pool.acquire() as conn:
        result = await conn.fetchval("select lexai_status_summary()")
    return result or {}


@public_router.get("/incidents")
async def status_incidents(limit: int = 30):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select i.*, (
              select coalesce(jsonb_agg(jsonb_build_object(
                'status', u.status, 'message', u.message, 'at', u.created_at
              ) order by u.created_at), '[]'::jsonb)
                from status_incident_updates u where u.incident_id = i.id
            ) as updates
              from status_incidents i
             order by i.started_at desc
             limit $1
            """, limit,
        )
    return {"items": [dict(r) for r in rows]}


# ══════════════════════════════════════════════════════════════════════
# ADMIN
# ══════════════════════════════════════════════════════════════════════


async def _probe_db() -> tuple[str, int | None, str | None]:
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return "major_outage", None, "no storage pool"
    t0 = time.time()
    try:
        async with storage.pool.acquire() as conn:
            await conn.fetchval("select 1")
        latency = int((time.time() - t0) * 1000)
        if latency > 1000:
            return "degraded", latency, f"slow: {latency}ms"
        return "operational", latency, None
    except Exception as e:
        return "major_outage", None, str(e)[:120]


async def _probe_openai() -> tuple[str, int | None, str | None]:
    import os
    if not os.getenv("OPENAI_API_KEY"):
        return "operational", None, "key not configured"
    import httpx
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
            )
        latency = int((time.time() - t0) * 1000)
        if r.status_code == 200:
            return "operational", latency, None
        return "degraded", latency, f"HTTP {r.status_code}"
    except Exception as e:
        return "major_outage", None, str(e)[:120]


async def _probe_storage() -> tuple[str, int | None, str | None]:
    """Verifica que Supabase Storage responde."""
    import os
    import httpx
    supabase_url = os.getenv("SUPABASE_URL")
    if not supabase_url:
        return "operational", None, "url not configured"
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{supabase_url}/storage/v1/bucket")
        latency = int((time.time() - t0) * 1000)
        if r.status_code in (200, 401):  # 401 = expected without auth, means reachable
            return "operational", latency, None
        return "degraded", latency, f"HTTP {r.status_code}"
    except Exception as e:
        return "major_outage", None, str(e)[:120]


@admin_router.post("/probe-now")
async def admin_probe_now(
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    """Corre los probes de todos los componentes ahora mismo.

    Útil para verificar manualmente. Worker corre cron Railway cada 5 min.
    """
    results = {}
    probes = [
        ("database", _probe_db),
        ("ai", _probe_openai),
        ("storage", _probe_storage),
    ]

    from utils.db import get_storage
    storage = await get_storage()

    for component_key, probe_fn in probes:
        status_val, latency, error = await probe_fn()
        results[component_key] = {"status": status_val, "latency_ms": latency, "error": error}

        if hasattr(storage, "pool"):
            try:
                async with storage.pool.acquire() as conn:
                    # Save check
                    await conn.execute(
                        """
                        insert into status_checks (component_key, status, latency_ms, error_text)
                        values ($1, $2, $3, $4)
                        """,
                        component_key, status_val, latency, error,
                    )
                    # Update current_status if changed
                    await conn.execute(
                        """
                        update status_components
                           set current_status = $2,
                               current_status_since = case
                                 when current_status != $2 then now()
                                 else current_status_since end
                         where key = $1
                        """,
                        component_key, status_val,
                    )
            except Exception as e:
                logger.warning("status probe save failed for %s: %s", component_key, e)

    await audit_admin_action(admin, "status.probe_now", request=request, metadata=results)
    return {"ok": True, "results": results}


class ComponentStatus(BaseModel):
    status: str = Field(pattern="^(operational|degraded|partial_outage|major_outage|maintenance)$")


@admin_router.post("/components/{key}/status")
async def admin_set_component_status(
    key: str, body: ComponentStatus, request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            update status_components set
              current_status = $2,
              current_status_since = case when current_status != $2 then now() else current_status_since end
             where key = $1
            """, key, body.status,
        )
    await audit_admin_action(admin, "status.component_status", resource_type="status_component",
                             resource_id=key, request=request, metadata=body.dict())
    return {"ok": True}


class IncidentCreate(BaseModel):
    title: str = Field(min_length=5, max_length=200)
    body: Optional[str] = None
    impact: str = Field(default="minor", pattern="^(minor|major|critical|maintenance)$")
    components: list[str] = []


@admin_router.post("/incidents")
async def admin_create_incident(
    body: IncidentCreate, request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into status_incidents (title, body, impact, components, created_by)
            values ($1, $2, $3, $4, $5::uuid)
            returning id
            """,
            body.title, body.body, body.impact, body.components, admin.admin_user_id,
        )
        await conn.execute(
            """
            insert into status_incident_updates (incident_id, status, message, created_by)
            values ($1::uuid, 'investigating', $2, $3::uuid)
            """,
            row["id"], body.body or "Investigando...", admin.admin_user_id,
        )
    await audit_admin_action(admin, "status.incident_create", resource_type="incident",
                             resource_id=str(row["id"]), request=request, metadata=body.dict())
    return {"ok": True, "id": str(row["id"])}


class IncidentUpdate(BaseModel):
    status: str = Field(pattern="^(investigating|identified|monitoring|resolved)$")
    message: str = Field(min_length=2, max_length=2000)


@admin_router.patch("/incidents/{incident_id}")
async def admin_update_incident(
    incident_id: str, body: IncidentUpdate, request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            update status_incidents set
              status = $2,
              resolved_at = case when $2 = 'resolved' then now() else resolved_at end,
              updated_at = now()
             where id = $1::uuid
            """,
            incident_id, body.status,
        )
        await conn.execute(
            """
            insert into status_incident_updates (incident_id, status, message, created_by)
            values ($1::uuid, $2, $3, $4::uuid)
            """,
            incident_id, body.status, body.message, admin.admin_user_id,
        )
    await audit_admin_action(admin, "status.incident_update", resource_type="incident",
                             resource_id=incident_id, request=request, metadata=body.dict())
    return {"ok": True}


@admin_router.get("/incidents")
async def admin_list_incidents(
    open_only: bool = False, limit: int = 50,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    where = "1=1"
    if open_only:
        where = "resolved_at is null"
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            f"select * from status_incidents where {where} order by started_at desc limit $1",
            limit,
        )
    return {"items": [dict(r) for r in rows]}
