"""Sprint 13 · Imports API.

  POST /v1/imports/upload                · multipart CSV → job + rows
  POST /v1/imports/{id}/validate         · corre dry-run (no escribe destino)
  POST /v1/imports/{id}/commit           · ejecuta el insert real
  POST /v1/imports/{id}/cancel
  GET  /v1/imports                       · lista
  GET  /v1/imports/{id}                  · job + counts + sample rows
  GET  /v1/imports/{id}/rows              · paginado
  GET  /v1/imports/fields/{kind}         · expone schema del kind
"""

from __future__ import annotations

import csv
import io
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/imports", tags=["imports"])


@router.get("/fields/{kind}")
async def fields(
    kind: str,
    _: Principal = Depends(get_current_firm),
):
    from agent.workers.import_processor import FIELDS_BY_KIND, HEURISTIC_MAP
    if kind not in FIELDS_BY_KIND:
        raise HTTPException(404, f"kind no soportado (válidos: {list(FIELDS_BY_KIND.keys())})")
    return {
        "kind": kind,
        "fields": FIELDS_BY_KIND[kind],
        "heuristic_aliases": HEURISTIC_MAP,
    }


@router.post("/upload")
async def upload(
    kind: str = Form(...),
    column_mapping: Optional[str] = Form(default=None),
    file: UploadFile = File(...),
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior", "socio_junior"):
        raise HTTPException(403, "Solo socios/admin pueden importar")
    from agent.workers.import_processor import FIELDS_BY_KIND
    if kind not in FIELDS_BY_KIND:
        raise HTTPException(400, "kind no soportado")
    mapping_dict: dict = {}
    if column_mapping:
        try:
            mapping_dict = json.loads(column_mapping)
            if not isinstance(mapping_dict, dict):
                raise ValueError("debe ser objeto")
        except Exception as e:
            raise HTTPException(400, f"column_mapping JSON inválido: {e}")

    raw = (await file.read()).decode("utf-8-sig", errors="ignore")
    if not raw.strip():
        raise HTTPException(400, "archivo vacío")

    delim = ";" if raw.count(";") > raw.count(",") else ","
    reader = csv.DictReader(io.StringIO(raw), delimiter=delim)
    rows = list(reader)
    if not rows:
        raise HTTPException(400, "CSV sin filas de datos")

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")

    async with storage.pool.acquire() as conn:
        job = await conn.fetchrow(
            """
            insert into import_jobs
              (firm_id, kind, source_filename, source_format, column_mapping,
               options, rows_total, created_by)
            values ($1::uuid, $2, $3, 'csv', $4::jsonb, $5::jsonb, $6, $7::uuid)
            returning id, kind, status, rows_total
            """,
            principal.firm_id, kind, file.filename,
            json.dumps(mapping_dict),
            json.dumps({"delimiter": delim}),
            len(rows), principal.user_id,
        )
        job_id = job["id"]
        for i, r in enumerate(rows, start=2):  # línea 1 = header
            await conn.execute(
                """
                insert into import_rows (firm_id, import_job_id, line_number, raw_payload)
                values ($1::uuid, $2::uuid, $3, $4::jsonb)
                """,
                principal.firm_id, job_id, i, json.dumps(r),
            )
    return {
        "job_id": str(job_id),
        "kind": kind,
        "rows_total": len(rows),
        "next_step": "POST /v1/imports/{job_id}/validate para preview",
    }


@router.post("/{job_id}/validate")
async def validate(
    job_id: str,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior", "socio_junior"):
        raise HTTPException(403, "Sin permisos")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        ok = await conn.fetchval(
            "select 1 from import_jobs where id = $1::uuid and firm_id = $2::uuid",
            job_id, principal.firm_id,
        )
    if not ok:
        raise HTTPException(404, "job no encontrado")
    from agent.workers.import_processor import process_job
    return await process_job(job_id, commit=False)


@router.post("/{job_id}/commit")
async def commit_job(
    job_id: str,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        # Resetear filas con status 'ok' a 'pending' para insertarlas
        await conn.execute(
            "update import_rows set status = 'pending', error = null, created_id = null "
            "where import_job_id = $1::uuid and status in ('ok','error')",
            job_id,
        )
    from agent.workers.import_processor import process_job
    return await process_job(job_id, commit=True)


@router.post("/{job_id}/cancel")
async def cancel(
    job_id: str,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update import_jobs set status = 'canceled', completed_at = now()
             where id = $1::uuid and firm_id = $2::uuid and status in ('pending','validating','validated')
            returning id
            """,
            job_id, principal.firm_id,
        )
    if not row:
        raise HTTPException(409, "no se puede cancelar en este estado")
    return {"id": str(row["id"]), "status": "canceled"}


@router.get("")
async def list_jobs(
    kind: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    where = ["firm_id = $1::uuid"]
    params: list = [principal.firm_id]
    if kind:
        params.append(kind); where.append(f"kind = ${len(params)}")
    params.append(limit)
    sql = f"""
        select id, kind, source_filename, status, rows_total, rows_ok, rows_error,
               rows_warnings, error_summary, started_at, completed_at, created_at
          from import_jobs
         where {' and '.join(where)}
         order by created_at desc
         limit ${len(params)}
    """
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]), "kind": r["kind"],
                "source_filename": r["source_filename"], "status": r["status"],
                "rows_total": r["rows_total"], "rows_ok": r["rows_ok"],
                "rows_error": r["rows_error"], "rows_warnings": r["rows_warnings"],
                "error_summary": r["error_summary"],
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


@router.get("/{job_id}")
async def get_job(
    job_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        job = await conn.fetchrow(
            """
            select id, kind, source_filename, column_mapping, options, status,
                   rows_total, rows_ok, rows_error, rows_warnings, error_summary,
                   started_at, completed_at, created_at
              from import_jobs
             where id = $1::uuid and firm_id = $2::uuid
            """,
            job_id, principal.firm_id,
        )
        if not job:
            raise HTTPException(404, "not found")
        sample = await conn.fetch(
            """
            select line_number, raw_payload, parsed_payload, status, error, created_id, warnings
              from import_rows
             where import_job_id = $1::uuid
             order by case status when 'error' then 1 when 'warning' then 2 else 3 end,
                      line_number
             limit 20
            """,
            job_id,
        )
    return {
        "job": {
            "id": str(job["id"]), "kind": job["kind"],
            "source_filename": job["source_filename"],
            "column_mapping": job["column_mapping"],
            "options": job["options"], "status": job["status"],
            "rows_total": job["rows_total"], "rows_ok": job["rows_ok"],
            "rows_error": job["rows_error"], "rows_warnings": job["rows_warnings"],
            "error_summary": job["error_summary"],
            "started_at": job["started_at"].isoformat() if job["started_at"] else None,
            "completed_at": job["completed_at"].isoformat() if job["completed_at"] else None,
            "created_at": job["created_at"].isoformat() if job["created_at"] else None,
        },
        "sample_rows": [
            {
                "line_number": r["line_number"],
                "raw_payload": r["raw_payload"],
                "parsed_payload": r["parsed_payload"],
                "status": r["status"],
                "error": r["error"],
                "created_id": str(r["created_id"]) if r["created_id"] else None,
                "warnings": r["warnings"],
            }
            for r in sample
        ],
    }


@router.get("/{job_id}/rows")
async def list_rows(
    job_id: str,
    status: Optional[str] = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, le=500),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    where = ["firm_id = $1::uuid", "import_job_id = $2::uuid"]
    params: list = [principal.firm_id, job_id]
    if status:
        params.append(status); where.append(f"status = ${len(params)}")
    params.append(limit)
    params.append(offset)
    sql = f"""
        select line_number, raw_payload, parsed_payload, status, error, created_id, warnings
          from import_rows
         where {' and '.join(where)}
         order by line_number
         limit ${len(params)-1} offset ${len(params)}
    """
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {
        "count": len(rows),
        "items": [
            {
                "line_number": r["line_number"],
                "raw_payload": r["raw_payload"],
                "parsed_payload": r["parsed_payload"],
                "status": r["status"],
                "error": r["error"],
                "created_id": str(r["created_id"]) if r["created_id"] else None,
                "warnings": r["warnings"],
            }
            for r in rows
        ],
    }


# ══════════════════════════════════════════════════════════════════════
# Voice tool
# ══════════════════════════════════════════════════════════════════════


async def import_csv_tool(args: dict, ctx: dict) -> dict:
    """Voice: 'LexAI, valida el import job X'."""
    firm_id = ctx.get("firm_id")
    job_id = args.get("job_id")
    if not (firm_id and job_id):
        return {"error": "firm_id y job_id requeridos"}
    commit = bool(args.get("commit"))
    from agent.workers.import_processor import process_job
    return await process_job(job_id, commit=commit)
