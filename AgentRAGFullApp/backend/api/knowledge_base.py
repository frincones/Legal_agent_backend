"""Sprint 15 · Knowledge Base API.

Endpoints:
  GET    /v1/kb/stats                · counters
  GET    /v1/kb/collections          · listar colecciones
  POST   /v1/kb/collections          · crear colección
  PATCH  /v1/kb/collections/{id}     · renombrar / mover
  DELETE /v1/kb/collections/{id}     · borrar (entries quedan sin collection)

  GET    /v1/kb/entries              · listar (filtros · kind, collection, tag, pinned)
  GET    /v1/kb/entries/{id}         · detalle (incrementa view_count + last_used_at)
  POST   /v1/kb/entries              · crear (embed sincrónico best-effort)
  PATCH  /v1/kb/entries/{id}         · actualizar (re-embed si cambia title/body)
  DELETE /v1/kb/entries/{id}
  POST   /v1/kb/entries/{id}/pin
  POST   /v1/kb/entries/{id}/unpin

  POST   /v1/kb/search               · híbrida vector + texto (RPC lexai_kb_search)

  GET    /v1/kb/annotations          · ?matter_document_id=... | ?matter_id=...
  POST   /v1/kb/annotations          · crear highlight/comentario
  DELETE /v1/kb/annotations/{id}

Multi-tenant: cada operación se filtra por `principal.firm_id`. RLS en
Postgres es la red de seguridad final, pero los WHERE explícitos evitan
cargar filas que luego serían filtradas.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/kb", tags=["knowledge_base"])


VALID_KINDS = {
    "note", "precedent", "strategy", "template_comment", "citation_note",
    "lesson_learned", "procedure", "case_summary", "contact_note",
}
VALID_VISIBILITY = {"private", "firm", "public"}
VALID_ANN_KINDS = {"highlight", "question", "important", "red_flag", "reference"}


# --------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------
@router.get("/stats")
async def kb_stats(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"entries_total": 0, "entries_embedded": 0, "lessons_total": 0}
    async with storage.pool.acquire() as conn:
        row = await conn.fetchval("select lexai_kb_stats($1::uuid)", principal.firm_id)
    if row is None:
        return {"entries_total": 0, "entries_embedded": 0, "lessons_total": 0}
    if isinstance(row, str):
        try:
            return json.loads(row)
        except Exception:
            return {}
    return row


# --------------------------------------------------------------------
# Collections
# --------------------------------------------------------------------
class CollectionIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    color: Optional[str] = "blue"
    parent_id: Optional[str] = None
    sort_order: int = 0


class CollectionPatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    color: Optional[str] = None
    parent_id: Optional[str] = None
    sort_order: Optional[int] = None


@router.get("/collections")
async def list_collections(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select c.id, c.name, c.description, c.color, c.parent_id, c.sort_order,
                   c.created_at,
                   (select count(*) from knowledge_entries e
                     where e.collection_id = c.id and e.firm_id = c.firm_id) as entry_count
              from kb_collections c
             where c.firm_id = $1::uuid
             order by c.parent_id nulls first, c.sort_order, c.name
            """,
            principal.firm_id,
        )
    return {
        "items": [
            {
                "id": str(r["id"]),
                "name": r["name"],
                "description": r["description"],
                "color": r["color"],
                "parent_id": str(r["parent_id"]) if r["parent_id"] else None,
                "sort_order": r["sort_order"],
                "entry_count": int(r["entry_count"] or 0),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
    }


@router.post("/collections", status_code=201)
async def create_collection(
    body: CollectionIn,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                insert into kb_collections
                  (firm_id, name, description, color, parent_id, sort_order, created_by)
                values ($1::uuid, $2, $3, $4, $5::uuid, $6, $7::uuid)
                returning id, name, color, parent_id, sort_order
                """,
                principal.firm_id, body.name, body.description, body.color or "blue",
                body.parent_id, body.sort_order, principal.user_id,
            )
        except Exception as e:
            msg = str(e).lower()
            if "unique" in msg or "duplicate" in msg:
                raise HTTPException(409, "Ya existe una colección con ese nombre")
            raise HTTPException(400, f"No se pudo crear: {e}")
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "color": row["color"],
        "parent_id": str(row["parent_id"]) if row["parent_id"] else None,
        "sort_order": row["sort_order"],
    }


@router.patch("/collections/{collection_id}")
async def update_collection(
    collection_id: str,
    body: CollectionPatch,
    principal: Principal = Depends(get_current_firm),
):
    sets: list[str] = []
    args: list = []
    idx = 1
    for col, val in (
        ("name", body.name), ("description", body.description), ("color", body.color),
        ("sort_order", body.sort_order),
    ):
        if val is not None:
            sets.append(f"{col} = ${idx}")
            args.append(val)
            idx += 1
    if body.parent_id is not None:
        sets.append(f"parent_id = ${idx}::uuid")
        args.append(body.parent_id or None)
        idx += 1
    if not sets:
        raise HTTPException(400, "Sin cambios")
    args.append(principal.firm_id)
    args.append(collection_id)
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            update kb_collections
               set {', '.join(sets)}
             where firm_id = ${idx}::uuid and id = ${idx + 1}::uuid
             returning id
            """,
            *args,
        )
    if not row:
        raise HTTPException(404, "Colección no encontrada")
    return {"id": str(row["id"]), "updated": True}


@router.delete("/collections/{collection_id}")
async def delete_collection(
    collection_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "delete from kb_collections where firm_id = $1::uuid and id = $2::uuid",
            principal.firm_id, collection_id,
        )
    return {"deleted": True}


# --------------------------------------------------------------------
# Entries
# --------------------------------------------------------------------
class EntryIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=400)
    body: str = Field(..., min_length=1)
    kind: str = "note"
    tags: list[str] = Field(default_factory=list)
    collection_id: Optional[str] = None
    source_matter_id: Optional[str] = None
    source_document_id: Optional[str] = None
    related_citations: list[str] = Field(default_factory=list)
    visibility: str = "firm"
    pinned: bool = False


class EntryPatch(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    kind: Optional[str] = None
    tags: Optional[list[str]] = None
    collection_id: Optional[str] = None
    related_citations: Optional[list[str]] = None
    visibility: Optional[str] = None
    pinned: Optional[bool] = None


def _serialize_entry(r) -> dict:
    return {
        "id": str(r["id"]),
        "title": r["title"],
        "body": r["body"],
        "kind": r["kind"],
        "tags": list(r["tags"] or []),
        "collection_id": str(r["collection_id"]) if r["collection_id"] else None,
        "source_matter_id": str(r["source_matter_id"]) if r["source_matter_id"] else None,
        "source_document_id": str(r["source_document_id"]) if r["source_document_id"] else None,
        "related_citations": list(r["related_citations"] or []) if not isinstance(r["related_citations"], str) else (json.loads(r["related_citations"]) if r["related_citations"] else []),
        "visibility": r["visibility"],
        "pinned": bool(r["pinned"]),
        "view_count": int(r["view_count"] or 0),
        "embedded": r["embedding_at"] is not None,
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


@router.get("/entries")
async def list_entries(
    kind: Optional[str] = Query(default=None),
    collection_id: Optional[str] = Query(default=None),
    tag: Optional[str] = Query(default=None),
    pinned: Optional[bool] = Query(default=None),
    search: Optional[str] = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_current_firm),
):
    where = ["firm_id = $1::uuid"]
    args: list = [principal.firm_id]
    idx = 2
    if kind:
        if kind not in VALID_KINDS:
            raise HTTPException(400, f"kind inválido (válidos: {sorted(VALID_KINDS)})")
        where.append(f"kind = ${idx}"); args.append(kind); idx += 1
    if collection_id:
        where.append(f"collection_id = ${idx}::uuid"); args.append(collection_id); idx += 1
    if tag:
        where.append(f"${idx} = ANY(tags)"); args.append(tag); idx += 1
    if pinned is not None:
        where.append(f"pinned = ${idx}"); args.append(pinned); idx += 1
    if search:
        where.append(
            f"(title ilike ${idx} or body ilike ${idx})"
        )
        args.append(f"%{search}%"); idx += 1
    args.extend([limit, offset])
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": [], "total": 0}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            select id, title, body, kind, tags, collection_id,
                   source_matter_id, source_document_id, related_citations,
                   visibility, pinned, view_count, embedding_at,
                   created_at, updated_at
              from knowledge_entries
             where {' and '.join(where)}
             order by pinned desc, updated_at desc
             limit ${idx} offset ${idx + 1}
            """,
            *args,
        )
    return {"items": [_serialize_entry(r) for r in rows], "count": len(rows)}


@router.get("/entries/{entry_id}")
async def get_entry(
    entry_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, title, body, kind, tags, collection_id,
                   source_matter_id, source_document_id, related_citations,
                   visibility, pinned, view_count, embedding_at,
                   created_at, updated_at
              from knowledge_entries
             where firm_id = $1::uuid and id = $2::uuid
            """,
            principal.firm_id, entry_id,
        )
        if not row:
            raise HTTPException(404, "Entrada no encontrada")
        # Best-effort bump del contador. No bloqueante.
        try:
            await conn.execute(
                "update knowledge_entries set view_count = view_count + 1, "
                "last_used_at = now() where firm_id = $1::uuid and id = $2::uuid",
                principal.firm_id, entry_id,
            )
        except Exception as e:
            logger.debug("view_count bump failed: %s", e)
    return _serialize_entry(row)


@router.post("/entries", status_code=201)
async def create_entry(
    body: EntryIn,
    principal: Principal = Depends(get_current_firm),
):
    if body.kind not in VALID_KINDS:
        raise HTTPException(400, f"kind inválido (válidos: {sorted(VALID_KINDS)})")
    if body.visibility not in VALID_VISIBILITY:
        raise HTTPException(400, f"visibility inválida (válidas: {sorted(VALID_VISIBILITY)})")
    from utils.db import get_storage
    from utils.embeddings import embed_text_as_pg, compose_kb_text
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")

    # Embed sincrónico best-effort (si falla, el worker kb_indexer lo recoge).
    embed_text_str = compose_kb_text(body.title, None, body.body)
    embedding_pg = await embed_text_as_pg(
        embed_text_str,
        purpose="kb_entry_create",
        session_id=str(principal.user_id) if principal.user_id else "",
    )

    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into knowledge_entries
              (firm_id, collection_id, kind, title, body, tags,
               source_matter_id, source_document_id, related_citations,
               visibility, pinned, embedding, embedding_at, created_by)
            values ($1::uuid, $2::uuid, $3, $4, $5, $6,
                    $7::uuid, $8::uuid, $9::jsonb,
                    $10, $11,
                    case when $12::text is not null then $12::vector else null end,
                    case when $12::text is not null then now() else null end,
                    $13::uuid)
            returning id, title, body, kind, tags, collection_id,
                      source_matter_id, source_document_id, related_citations,
                      visibility, pinned, view_count, embedding_at,
                      created_at, updated_at
            """,
            principal.firm_id, body.collection_id, body.kind, body.title, body.body,
            body.tags or [], body.source_matter_id, body.source_document_id,
            json.dumps(body.related_citations or []),
            body.visibility, body.pinned,
            embedding_pg, principal.user_id,
        )
    return _serialize_entry(row)


@router.patch("/entries/{entry_id}")
async def update_entry(
    entry_id: str,
    body: EntryPatch,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    from utils.embeddings import embed_text_as_pg, compose_kb_text
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")

    sets: list[str] = []
    args: list = []
    idx = 1
    re_embed = False
    for col, val in (
        ("title", body.title), ("body", body.body), ("kind", body.kind),
        ("visibility", body.visibility), ("pinned", body.pinned),
    ):
        if val is None:
            continue
        if col == "kind" and val not in VALID_KINDS:
            raise HTTPException(400, "kind inválido")
        if col == "visibility" and val not in VALID_VISIBILITY:
            raise HTTPException(400, "visibility inválida")
        sets.append(f"{col} = ${idx}")
        args.append(val); idx += 1
        if col in ("title", "body"):
            re_embed = True
    if body.tags is not None:
        sets.append(f"tags = ${idx}")
        args.append(body.tags); idx += 1
    if body.collection_id is not None:
        sets.append(f"collection_id = ${idx}::uuid")
        args.append(body.collection_id or None); idx += 1
    if body.related_citations is not None:
        sets.append(f"related_citations = ${idx}::jsonb")
        args.append(json.dumps(body.related_citations)); idx += 1
    if not sets and not re_embed:
        raise HTTPException(400, "Sin cambios")

    async with storage.pool.acquire() as conn:
        if re_embed:
            current = await conn.fetchrow(
                "select title, body from knowledge_entries where firm_id = $1::uuid and id = $2::uuid",
                principal.firm_id, entry_id,
            )
            if not current:
                raise HTTPException(404, "Entrada no encontrada")
            new_title = body.title if body.title is not None else current["title"]
            new_body = body.body if body.body is not None else current["body"]
            embedding_pg = await embed_text_as_pg(
                compose_kb_text(new_title, None, new_body),
                purpose="kb_entry_update",
                session_id=str(principal.user_id) if principal.user_id else "",
            )
            if embedding_pg:
                sets.append(f"embedding = ${idx}::vector")
                args.append(embedding_pg); idx += 1
                sets.append("embedding_at = now()")
            else:
                # Si falló el embed, marcamos como pendiente para que el worker lo recoja.
                sets.append("embedding = null")
                sets.append("embedding_at = null")
        args.append(principal.firm_id)
        args.append(entry_id)
        row = await conn.fetchrow(
            f"""
            update knowledge_entries
               set {', '.join(sets)}
             where firm_id = ${idx}::uuid and id = ${idx + 1}::uuid
             returning id, title, body, kind, tags, collection_id,
                       source_matter_id, source_document_id, related_citations,
                       visibility, pinned, view_count, embedding_at,
                       created_at, updated_at
            """,
            *args,
        )
    if not row:
        raise HTTPException(404, "Entrada no encontrada")
    return _serialize_entry(row)


@router.delete("/entries/{entry_id}")
async def delete_entry(
    entry_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        result = await conn.execute(
            "delete from knowledge_entries where firm_id = $1::uuid and id = $2::uuid",
            principal.firm_id, entry_id,
        )
    return {"deleted": True, "result": result}


@router.post("/entries/{entry_id}/pin")
async def pin_entry(
    entry_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "update knowledge_entries set pinned = true where firm_id = $1::uuid and id = $2::uuid",
            principal.firm_id, entry_id,
        )
    return {"pinned": True}


@router.post("/entries/{entry_id}/unpin")
async def unpin_entry(
    entry_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "update knowledge_entries set pinned = false where firm_id = $1::uuid and id = $2::uuid",
            principal.firm_id, entry_id,
        )
    return {"pinned": False}


# --------------------------------------------------------------------
# Search · híbrida vector + texto
# --------------------------------------------------------------------
class SearchIn(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    kind: Optional[str] = None
    limit: int = Field(default=15, ge=1, le=50)
    use_vector: bool = True


@router.post("/search")
async def kb_search(
    body: SearchIn,
    principal: Principal = Depends(get_current_firm),
):
    if body.kind and body.kind not in VALID_KINDS:
        raise HTTPException(400, f"kind inválido (válidos: {sorted(VALID_KINDS)})")
    from utils.db import get_storage
    from utils.embeddings import embed_text_as_pg
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    embedding_pg = None
    if body.use_vector:
        embedding_pg = await embed_text_as_pg(
            body.query,
            purpose="kb_search",
            session_id=str(principal.user_id) if principal.user_id else "",
        )
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select * from lexai_kb_search(
              $1::uuid, $2,
              case when $3::text is not null then $3::vector else null end,
              $4, $5
            )
            """,
            principal.firm_id, body.query, embedding_pg, body.kind, body.limit,
        )
    return {
        "items": [
            {
                "id": str(r["id"]),
                "title": r["title"],
                "body": r["body"],
                "kind": r["kind"],
                "tags": list(r["tags"] or []),
                "source_matter_id": str(r["source_matter_id"]) if r["source_matter_id"] else None,
                "source_document_id": str(r["source_document_id"]) if r["source_document_id"] else None,
                "pinned": bool(r["pinned"]),
                "rank": float(r["rank"] or 0),
            }
            for r in rows
        ],
        "vector_used": embedding_pg is not None,
    }


# --------------------------------------------------------------------
# Annotations
# --------------------------------------------------------------------
class AnnotationIn(BaseModel):
    matter_document_id: str
    matter_id: Optional[str] = None
    page: Optional[int] = None
    text_quote: Optional[str] = None
    body: str = Field(..., min_length=1)
    color: Optional[str] = "yellow"
    kind: Optional[str] = "highlight"


@router.get("/annotations")
async def list_annotations(
    matter_document_id: Optional[str] = Query(default=None),
    matter_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_current_firm),
):
    if not matter_document_id and not matter_id:
        raise HTTPException(400, "Indica matter_document_id o matter_id")
    where = ["firm_id = $1::uuid"]
    args: list = [principal.firm_id]
    idx = 2
    if matter_document_id:
        where.append(f"matter_document_id = ${idx}::uuid")
        args.append(matter_document_id); idx += 1
    if matter_id:
        where.append(f"matter_id = ${idx}::uuid")
        args.append(matter_id); idx += 1
    args.append(limit)
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            select id, matter_document_id, matter_id, user_id, page,
                   text_quote, body, color, kind, created_at
              from kb_annotations
             where {' and '.join(where)}
             order by created_at desc
             limit ${idx}
            """,
            *args,
        )
    return {
        "items": [
            {
                "id": str(r["id"]),
                "matter_document_id": str(r["matter_document_id"]),
                "matter_id": str(r["matter_id"]) if r["matter_id"] else None,
                "user_id": str(r["user_id"]) if r["user_id"] else None,
                "page": r["page"],
                "text_quote": r["text_quote"],
                "body": r["body"],
                "color": r["color"],
                "kind": r["kind"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
    }


@router.post("/annotations", status_code=201)
async def create_annotation(
    body: AnnotationIn,
    principal: Principal = Depends(get_current_firm),
):
    if body.kind and body.kind not in VALID_ANN_KINDS:
        raise HTTPException(400, f"kind inválido (válidos: {sorted(VALID_ANN_KINDS)})")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into kb_annotations
              (firm_id, matter_document_id, matter_id, user_id, page,
               text_quote, body, color, kind)
            values ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5,
                    $6, $7, $8, $9)
            returning id, created_at
            """,
            principal.firm_id, body.matter_document_id, body.matter_id,
            principal.user_id, body.page, body.text_quote, body.body,
            body.color or "yellow", body.kind or "highlight",
        )
    return {
        "id": str(row["id"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


@router.delete("/annotations/{annotation_id}")
async def delete_annotation(
    annotation_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "delete from kb_annotations where firm_id = $1::uuid and id = $2::uuid",
            principal.firm_id, annotation_id,
        )
    return {"deleted": True}
