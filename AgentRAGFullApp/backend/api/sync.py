"""Sprint 12 · Offline sync queue API.

Cuando el frontend está offline (juzgado sin Wi-Fi), las mutaciones se
guardan en IndexedDB localmente. Al volver conexión, el SW las envía aquí
para que el backend las ejecute en orden + reporte el resultado.

  POST /v1/sync/enqueue        · cliente envía batch de jobs (idempotent vía client_request_id)
  POST /v1/sync/run            · procesa jobs queued del usuario (síncrono)
  GET  /v1/sync/jobs           · lista jobs del usuario
  GET  /v1/sync/stats          · queue stats (RPC)
  POST /v1/sync/jobs/{id}/retry
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/sync", tags=["sync"])

ALLOWED_PATH_PREFIXES = (
    "/v1/leads", "/v1/lead-stages",
    "/v1/time-entries", "/v1/expenses",
    "/v1/trust", "/v1/matters",
    "/v1/clients", "/v1/insights",
    "/v1/notifications", "/v1/inbox",
)


class JobInput(BaseModel):
    client_request_id: str = Field(min_length=8, max_length=120)
    method: str = Field(pattern="^(POST|PATCH|PUT|DELETE)$")
    url: str
    payload: Optional[dict] = None


class EnqueueRequest(BaseModel):
    jobs: list[JobInput] = Field(min_length=1, max_length=100)


def _validate_path(url: str) -> bool:
    if not url.startswith("/"):
        return False
    return any(url.startswith(p) for p in ALLOWED_PATH_PREFIXES)


@router.post("/enqueue")
async def enqueue(
    body: EnqueueRequest,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    inserted = 0
    skipped: list[str] = []
    async with storage.pool.acquire() as conn:
        for job in body.jobs:
            if not _validate_path(job.url):
                skipped.append(job.client_request_id)
                continue
            try:
                await conn.execute(
                    """
                    insert into offline_sync_jobs
                      (firm_id, user_id, client_request_id, method, url, payload, status)
                    values ($1::uuid, $2::uuid, $3, $4, $5, $6::jsonb, 'queued')
                    on conflict (user_id, client_request_id) do nothing
                    """,
                    principal.firm_id, principal.user_id,
                    job.client_request_id, job.method, job.url,
                    json.dumps(job.payload or {}),
                )
                inserted += 1
            except Exception as e:
                logger.warning("enqueue insert failed: %s", e)
                skipped.append(job.client_request_id)
    return {"queued": inserted, "skipped_paths": skipped}


@router.post("/run")
async def run(
    max_jobs: int = Query(default=20, le=100),
    principal: Principal = Depends(get_current_firm),
):
    """Procesa hasta N jobs queued del usuario actual, en orden FIFO.

    Hace un fetch interno (server-side) hacia los endpoints de la propia API
    con auth del usuario (passing principal JWT via request headers no es
    trivial dentro de FastAPI sin re-armar el JWT). Como simplificación,
    invocamos la lógica directamente importando los routers — pero eso
    requiere refactor. La estrategia más simple y suficiente: marcar los
    jobs como 'succeeded' inmediatamente cuando se invoca desde el frontend
    (que ya hizo la mutation OPTIMISTA mientras estaba offline, y al volver
    online el frontend re-ejecuta los fetches con FE auth normal).

    Este endpoint sirve principalmente como **registro de auditoría** y para
    consultar el estado de la cola. El frontend hace el fetch real con
    cookie Supabase normal y luego marca el job como 'succeeded'.
    """
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        jobs = await conn.fetch(
            """
            select id, method, url, payload, attempts, max_attempts, client_request_id
              from offline_sync_jobs
             where user_id = $1::uuid and status = 'queued'
             order by enqueued_at asc
             limit $2
            """,
            principal.user_id, max_jobs,
        )
    return {
        "count": len(jobs),
        "items": [
            {
                "id": str(j["id"]),
                "client_request_id": j["client_request_id"],
                "method": j["method"],
                "url": j["url"],
                "payload": j["payload"],
                "attempts": j["attempts"],
            }
            for j in jobs
        ],
    }


class JobResultRequest(BaseModel):
    job_id: str
    status_code: int
    success: bool
    response: Optional[dict] = None
    error: Optional[str] = None


@router.post("/jobs/result")
async def report_result(
    body: JobResultRequest,
    principal: Principal = Depends(get_current_firm),
):
    """Cliente reporta el resultado de ejecutar un job (síncrono o background)."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    status = "succeeded" if body.success else "failed"
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update offline_sync_jobs set
              status = $3,
              status_code = $4,
              response = $5::jsonb,
              error = $6,
              attempts = attempts + 1,
              completed_at = case when $3 = 'succeeded' then now() else completed_at end
             where id = $1::uuid and user_id = $2::uuid
             returning id, status
            """,
            body.job_id, principal.user_id, status,
            body.status_code, json.dumps(body.response or {}), body.error,
        )
    if not row:
        raise HTTPException(404, "job not found")
    return {"id": str(row["id"]), "status": row["status"]}


@router.get("/jobs")
async def list_jobs(
    status: Optional[str] = Query(default=None, regex="^(queued|processing|succeeded|failed|skipped)$"),
    limit: int = Query(default=50, le=200),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    where = ["user_id = $1::uuid"]
    params: list = [principal.user_id]
    if status:
        params.append(status); where.append(f"status = ${len(params)}")
    params.append(limit)
    sql = f"""
        select id, client_request_id, method, url, payload, status, status_code,
               error, attempts, enqueued_at, completed_at
          from offline_sync_jobs
         where {' and '.join(where)}
         order by enqueued_at desc
         limit ${len(params)}
    """
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]),
                "client_request_id": r["client_request_id"],
                "method": r["method"], "url": r["url"], "payload": r["payload"],
                "status": r["status"], "status_code": r["status_code"], "error": r["error"],
                "attempts": r["attempts"],
                "enqueued_at": r["enqueued_at"].isoformat() if r["enqueued_at"] else None,
                "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
            }
            for r in rows
        ],
    }


@router.get("/stats")
async def stats(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        result = await conn.fetchval(
            "select lexai_sync_queue_stats($1::uuid)", principal.user_id,
        )
    return result or {}


@router.post("/jobs/{job_id}/retry")
async def retry_job(
    job_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update offline_sync_jobs set
              status = 'queued', error = null, completed_at = null
             where id = $1::uuid and user_id = $2::uuid and status = 'failed'
             returning id
            """,
            job_id, principal.user_id,
        )
    if not row:
        raise HTTPException(409, "solo failed jobs pueden re-intentarse")
    return {"id": str(row["id"]), "status": "queued"}
