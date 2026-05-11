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
    # Sprint 2 · S2-02: granular procedural state
    instance: Optional[str] = Field(
        None,
        pattern="^(inicial|administrativa|primera|apelacion|casacion|firme)$",
    )
    proceso_tipo: Optional[str] = None
    current_term_due_at: Optional[datetime] = None


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
    # Sprint 2
    instance: Optional[str] = None
    proceso_tipo: Optional[str] = None
    current_term_due_at: Optional[datetime] = None


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
        instance=_safe_get(r, "instance"),
        proceso_tipo=_safe_get(r, "proceso_tipo"),
        current_term_due_at=_safe_get(r, "current_term_due_at"),
    )


def _safe_get(r, key, default=None):
    """Defensive accessor for asyncpg.Record-like rows that may or may
    not include a column (older queries don't select the new fields)."""
    try:
        return r[key]
    except (KeyError, IndexError):
        return default


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

    # Whitelist of legal columns to avoid SQL injection via field names.
    ALLOWED = {
        "titulo", "etapa_procesal", "tribunal", "juzgado", "expediente",
        "status", "priority", "proxima_fecha", "proxima_tipo", "cuantia",
        "pendientes", "metadata", "materia",
        # Sprint 2
        "instance", "proceso_tipo", "current_term_due_at",
    }
    sets = []
    args: list = []
    i = 1
    for k, v in fields.items():
        if k not in ALLOWED:
            continue  # silently drop unknown keys
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
    if not sets:
        raise HTTPException(400, "no valid fields to update")
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
# Sprint 2 · S2-01 · Citations / Risks per matter
# ─────────────────────────────────────────────────────────────────────


@router.get("/{matter_id}/citations")
async def list_matter_citations(
    matter_id: str,
    principal: Principal = Depends(get_current_firm),
    limit: int = Query(default=100, le=500),
):
    """Returns citations across all documents of the matter.
    Joins document_citations → matter_documents to filter by matter_id."""
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select c.id, c.matter_document_id, c.citation_ref,
                   c.rubro_inserted, c.estado, c.match_score,
                   c.inserted_at, c.verified_at,
                   d.titulo as document_titulo
            from document_citations c
            join matter_documents d on d.id = c.matter_document_id
            where d.matter_id = $1::uuid and d.firm_id = $2::uuid
            order by c.inserted_at desc
            limit $3
            """,
            matter_id, principal.firm_id, limit,
        )
    return {"items": [
        {
            "id": str(r["id"]),
            "matter_document_id": str(r["matter_document_id"]),
            "document_titulo": r["document_titulo"],
            "citation_ref": r["citation_ref"],
            "rubro_inserted": r["rubro_inserted"],
            "estado": r["estado"],
            "match_score": float(r["match_score"]) if r["match_score"] is not None else None,
            "inserted_at": r["inserted_at"].isoformat() if r["inserted_at"] else None,
            "verified_at": r["verified_at"].isoformat() if r["verified_at"] else None,
        } for r in rows
    ]}


@router.get("/{matter_id}/risks")
async def list_matter_risks(
    matter_id: str,
    principal: Principal = Depends(get_current_firm),
    include_resolved: bool = False,
):
    """Returns case_risks rows. M32 fully populates these via worker;
    until then this list will be empty for new matters."""
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        if include_resolved:
            rows = await conn.fetch(
                """
                select id, type, severity, title, description, evidence_url,
                       mitigation, detected_by, detected_at, resolved_at
                from case_risks
                where matter_id = $1::uuid and firm_id = $2::uuid
                order by detected_at desc
                """,
                matter_id, principal.firm_id,
            )
        else:
            rows = await conn.fetch(
                """
                select id, type, severity, title, description, evidence_url,
                       mitigation, detected_by, detected_at, resolved_at
                from case_risks
                where matter_id = $1::uuid and firm_id = $2::uuid
                  and resolved_at is null
                order by severity desc, detected_at desc
                """,
                matter_id, principal.firm_id,
            )
    return {"items": [
        {
            "id": str(r["id"]),
            "type": r["type"],
            "severity": int(r["severity"]),
            "title": r["title"],
            "description": r["description"],
            "evidence_url": r["evidence_url"],
            "mitigation": r["mitigation"],
            "detected_by": r["detected_by"],
            "detected_at": r["detected_at"].isoformat() if r["detected_at"] else None,
            "resolved_at": r["resolved_at"].isoformat() if r["resolved_at"] else None,
        } for r in rows
    ]}


# ─────────────────────────────────────────────────────────────────────
# Sprint 2 · S2-03 · Auto-rebuild timeline (LLM-extracted dates from docs)
# ─────────────────────────────────────────────────────────────────────


@router.post("/{matter_id}/timeline/auto-rebuild")
async def rebuild_timeline(
    matter_id: str,
    principal: Principal = Depends(get_current_firm),
):
    """Walks the matter's documents and inserts timeline events for
    each one not yet represented. Heuristic-based (uses doc.created_at
    + kind), so it's deterministic and cheap. A full LLM-extraction
    pass that mines dates from doc.resumen_ia/contenido is queued in
    Sprint 5 (court watcher integration).
    """
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        # Verify matter belongs to firm.
        matter = await conn.fetchrow(
            "select id from matters where id = $1::uuid and firm_id = $2::uuid",
            matter_id, principal.firm_id,
        )
        if not matter:
            raise HTTPException(404, "matter not found")

        docs = await conn.fetch(
            """
            select id, kind, titulo, status, created_at
            from matter_documents
            where matter_id = $1::uuid and firm_id = $2::uuid
            order by created_at asc
            """,
            matter_id, principal.firm_id,
        )

        # Existing timeline doc-related events (we use payload.matter_document_id
        # in metadata to avoid duplicates).
        existing = await conn.fetch(
            """
            select payload->>'matter_document_id' as doc_id, kind
            from matter_timeline
            where matter_id = $1::uuid and kind = 'documento_recibido'
            """,
            matter_id,
        )
        seen = {(r["doc_id"], r["kind"]) for r in existing if r["doc_id"]}

        inserted = 0
        for d in docs:
            key = (str(d["id"]), "documento_recibido")
            if key in seen:
                continue
            await conn.execute(
                """
                insert into matter_timeline
                  (firm_id, matter_id, ts, kind, actor_user_id, payload)
                values
                  ($1::uuid, $2::uuid, $3, 'documento_recibido', $4::uuid, $5::jsonb)
                """,
                principal.firm_id,
                matter_id,
                d["created_at"],
                principal.user_id,
                json.dumps({
                    "descripcion": f"{d['kind'].title()}: {d['titulo']}",
                    "matter_document_id": str(d["id"]),
                    "doc_kind": d["kind"],
                    "doc_status": d["status"],
                    "auto_rebuilt": True,
                }),
            )
            inserted += 1

    return {"inserted": inserted, "scanned": len(docs)}


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
