"""Matters API · multi-tenant CRUD over `matters` table.

All queries scoped by firm_id (RLS enforced via Postgres + defense-in-depth
in the WHERE clauses below). Read endpoints are RSC-friendly (no streaming).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/matters", tags=["matters"])


class MatterCreate(BaseModel):
    client_id: str
    titulo: str = Field(min_length=2)
    materia: str
    etapa_procesal: Optional[str] = None
    tribunal: Optional[str] = None
    juzgado: Optional[str] = None
    expediente: Optional[str] = None
    priority: str = Field(default="media", pattern="^(alta|media|baja)$")
    proxima_fecha: Optional[datetime] = None
    proxima_tipo: Optional[str] = None
    cuantia: Optional[float] = None
    is_demo: bool = False
    metadata: dict = Field(default_factory=dict)


class MatterUpdate(BaseModel):
    titulo: Optional[str] = None
    etapa_procesal: Optional[str] = None
    tribunal: Optional[str] = None
    juzgado: Optional[str] = None
    expediente: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    proxima_fecha: Optional[datetime] = None
    proxima_tipo: Optional[str] = None
    cuantia: Optional[float] = None
    pendientes: Optional[int] = None
    metadata: Optional[dict] = None


class MatterRow(BaseModel):
    id: str
    display_id: str
    client_id: str
    titulo: str
    materia: str
    etapa_procesal: Optional[str]
    tribunal: Optional[str]
    expediente: Optional[str]
    status: str
    priority: str
    proxima_fecha: Optional[datetime]
    proxima_tipo: Optional[str]
    cuantia: Optional[float]
    pendientes: int
    is_demo: bool
    created_at: datetime
    updated_at: datetime


def _row_to_matter(r) -> MatterRow:
    return MatterRow(
        id=str(r["id"]),
        display_id=r["display_id"],
        client_id=str(r["client_id"]),
        titulo=r["titulo"],
        materia=str(r["materia"]),
        etapa_procesal=r["etapa_procesal"],
        tribunal=r["tribunal"],
        expediente=r["expediente"],
        status=str(r["status"]),
        priority=str(r["priority"]),
        proxima_fecha=r["proxima_fecha"],
        proxima_tipo=r["proxima_tipo"],
        cuantia=float(r["cuantia"]) if r["cuantia"] is not None else None,
        pendientes=r["pendientes"],
        is_demo=r["is_demo"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


def _next_display_id(year: int, seq: int) -> str:
    return f"C-{year}-{seq:04d}"


@router.get("/", response_model=list[MatterRow])
async def list_matters(
    principal: Principal = Depends(get_current_firm),
    materia: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    offset: int = 0,
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, display_id, client_id, titulo, materia, etapa_procesal,
                   tribunal, expediente, status, priority, proxima_fecha,
                   proxima_tipo, cuantia, pendientes, is_demo, created_at, updated_at
            from matters
            where firm_id = $1::uuid
              and ($2::text is null or materia::text = $2)
              and ($3::text is null or status::text = $3)
            order by coalesce(proxima_fecha, created_at) asc
            limit $4 offset $5
            """,
            principal.firm_id, materia, status, limit, offset,
        )
    return [_row_to_matter(r) for r in rows]


@router.post("/", response_model=MatterRow, status_code=201)
async def create_matter(
    body: MatterCreate,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    matter_id = str(uuid.uuid4())
    year = datetime.utcnow().year
    async with storage.pool.acquire() as conn:
        # display_id sequence per firm/year — coarse but enough for MVP
        seq = await conn.fetchval(
            "select coalesce(max(substring(display_id from 'C-\\d{4}-(\\d+)$')::int), 0) + 1 "
            "from matters where firm_id = $1::uuid and display_id like $2",
            principal.firm_id, f"C-{year}-%",
        )
        display_id = _next_display_id(year, seq or 1)
        row = await conn.fetchrow(
            """
            insert into matters
              (id, firm_id, client_id, display_id, titulo, materia, etapa_procesal,
               tribunal, juzgado, expediente, priority, proxima_fecha, proxima_tipo,
               cuantia, is_demo, owner_user_id, metadata)
            values
              ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6::materia_legal, $7,
               $8, $9, $10, $11::matter_priority, $12, $13, $14, $15, $16::uuid, $17::jsonb)
            returning id, display_id, client_id, titulo, materia, etapa_procesal,
                      tribunal, expediente, status, priority, proxima_fecha, proxima_tipo,
                      cuantia, pendientes, is_demo, created_at, updated_at
            """,
            matter_id,
            principal.firm_id,
            body.client_id,
            display_id,
            body.titulo,
            body.materia,
            body.etapa_procesal,
            body.tribunal,
            body.juzgado,
            body.expediente,
            body.priority,
            body.proxima_fecha,
            body.proxima_tipo,
            body.cuantia,
            body.is_demo,
            principal.user_id,
            json.dumps(body.metadata),
        )
    return _row_to_matter(row)


@router.get("/{matter_id}", response_model=MatterRow)
async def get_matter(
    matter_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, display_id, client_id, titulo, materia, etapa_procesal,
                   tribunal, expediente, status, priority, proxima_fecha, proxima_tipo,
                   cuantia, pendientes, is_demo, created_at, updated_at
            from matters
            where id = $1::uuid and firm_id = $2::uuid
            """,
            matter_id, principal.firm_id,
        )
    if not row:
        raise HTTPException(404, "matter not found")
    return _row_to_matter(row)


@router.patch("/{matter_id}", response_model=MatterRow)
async def update_matter(
    matter_id: str,
    body: MatterUpdate,
    principal: Principal = Depends(get_current_firm),
):
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not fields:
        raise HTTPException(400, "no fields to update")

    sets = []
    args: list = []
    i = 1
    for k, v in fields.items():
        if k == "metadata":
            sets.append(f"metadata = ${i}::jsonb")
            args.append(json.dumps(v))
        elif k == "materia":
            sets.append(f"materia = ${i}::materia_legal")
            args.append(v)
        elif k == "status":
            sets.append(f"status = ${i}::matter_status")
            args.append(v)
        elif k == "priority":
            sets.append(f"priority = ${i}::matter_priority")
            args.append(v)
        else:
            sets.append(f"{k} = ${i}")
            args.append(v)
        i += 1
    args.append(matter_id)
    args.append(principal.firm_id)

    sql = (
        f"update matters set {', '.join(sets)} "
        f"where id = ${i}::uuid and firm_id = ${i+1}::uuid "
        f"returning id, display_id, client_id, titulo, materia, etapa_procesal, "
        f"tribunal, expediente, status, priority, proxima_fecha, proxima_tipo, "
        f"cuantia, pendientes, is_demo, created_at, updated_at"
    )
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(sql, *args)
    if not row:
        raise HTTPException(404, "matter not found")
    return _row_to_matter(row)


@router.get("/{matter_id}/timeline")
async def get_timeline(
    matter_id: str,
    principal: Principal = Depends(get_current_firm),
    limit: int = 50,
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, ts, kind, actor_user_id, agent_run_id, payload
            from matter_timeline
            where matter_id = $1::uuid and firm_id = $2::uuid
            order by ts desc
            limit $3
            """,
            matter_id, principal.firm_id, limit,
        )
    return {"items": [
        {
            "id": str(r["id"]),
            "ts": r["ts"].isoformat(),
            "kind": r["kind"],
            "actor_user_id": str(r["actor_user_id"]) if r["actor_user_id"] else None,
            "agent_run_id": str(r["agent_run_id"]) if r["agent_run_id"] else None,
            "payload": r["payload"] or {},
        } for r in rows
    ]}


# ─────────────────────────────────────────────────────────────────────
# Tool adapter for Realtime: open_matter_context
# ─────────────────────────────────────────────────────────────────────


async def open_matter_context_tool(args: dict, ctx: dict) -> dict:
    """Returns the slim case context the voice agent uses.

    Same data the Caso detail screen needs in the right rail.
    """
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    firm_id = ctx.get("firm_id")
    if not matter_id or not firm_id:
        return {"error": "matter_id and firm_id required"}
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage not available"}
    async with storage.pool.acquire() as conn:
        m = await conn.fetchrow(
            """
            select m.id, m.display_id, m.titulo, m.materia, m.etapa_procesal,
                   m.tribunal, m.expediente, m.priority, m.cuantia,
                   c.nombre as cliente_nombre, c.tax_id as cliente_tax_id
            from matters m
            join clients c on c.id = m.client_id
            where m.id = $1::uuid and m.firm_id = $2::uuid
            """,
            matter_id, firm_id,
        )
        if not m:
            return {"error": "matter not found"}
        parties = await conn.fetch(
            """
            select rol, nombre, tax_id from matter_parties
            where matter_id = $1::uuid and firm_id = $2::uuid
            """,
            matter_id, firm_id,
        )
        deadlines = await conn.fetch(
            """
            select titulo, fecha, tipo from matter_deadlines
            where matter_id = $1::uuid and firm_id = $2::uuid and completado = false
            order by fecha asc limit 10
            """,
            matter_id, firm_id,
        )
        timeline = await conn.fetch(
            """
            select kind, ts, payload from matter_timeline
            where matter_id = $1::uuid and firm_id = $2::uuid
            order by ts desc limit 20
            """,
            matter_id, firm_id,
        )
    return {
        "matter": {
            "id": str(m["id"]),
            "display_id": m["display_id"],
            "titulo": m["titulo"],
            "materia": str(m["materia"]),
            "etapa": m["etapa_procesal"],
            "tribunal": m["tribunal"],
            "expediente": m["expediente"],
            "priority": str(m["priority"]),
            "cuantia": float(m["cuantia"]) if m["cuantia"] else None,
            "cliente": {"nombre": m["cliente_nombre"], "tax_id": m["cliente_tax_id"]},
        },
        "partes": [{"rol": r["rol"], "nombre": r["nombre"], "tax_id": r["tax_id"]} for r in parties],
        "deadlines_pendientes": [
            {"titulo": r["titulo"], "fecha": r["fecha"].isoformat(), "tipo": r["tipo"]}
            for r in deadlines
        ],
        "timeline_reciente": [
            {"kind": r["kind"], "ts": r["ts"].isoformat(), "payload": r["payload"] or {}}
            for r in timeline
        ],
    }
