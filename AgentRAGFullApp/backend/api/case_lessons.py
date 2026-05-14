"""Sprint 15 · Case Lessons API (memoria del despacho).

Endpoints:
  GET    /v1/lessons                     · listar con filtros (outcome, tag, matter)
  GET    /v1/lessons/{id}
  POST   /v1/lessons                     · crear manual (un abogado escribe la lección)
  PATCH  /v1/lessons/{id}                · editar / revisar
  DELETE /v1/lessons/{id}

  POST   /v1/lessons/extract             · LLM extrae lecciones de un matter
                                          { matter_id } → llama agent.tools.extract_lessons
  POST   /v1/lessons/search              · búsqueda semántica (cosine, RPC lexai_lessons_search)
  GET    /v1/lessons/matter/{matter_id}  · lessons de un caso específico
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/lessons", tags=["case_lessons"])


VALID_OUTCOMES = {"won", "lost", "settled", "abandoned", "unknown"}


# --------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------
class LessonIn(BaseModel):
    matter_id: str
    outcome: str = "unknown"
    summary: str = Field(..., min_length=1)
    strategy_used: Optional[str] = None
    what_worked: Optional[str] = None
    what_failed: Optional[str] = None
    key_citations: list[str] = Field(default_factory=list)
    key_arguments: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class LessonPatch(BaseModel):
    outcome: Optional[str] = None
    summary: Optional[str] = None
    strategy_used: Optional[str] = None
    what_worked: Optional[str] = None
    what_failed: Optional[str] = None
    key_citations: Optional[list[str]] = None
    key_arguments: Optional[list[str]] = None
    tags: Optional[list[str]] = None
    mark_reviewed: bool = False


class ExtractIn(BaseModel):
    matter_id: str
    force: bool = False  # re-extraer aunque ya exista una llm_curated


class LessonSearchIn(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    outcome: Optional[str] = None
    limit: int = Field(default=10, ge=1, le=50)


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------
def _serialize_lesson(r) -> dict:
    def _arr(v):
        if v is None:
            return []
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return []
        return list(v)
    return {
        "id": str(r["id"]),
        "matter_id": str(r["matter_id"]),
        "outcome": r["outcome"],
        "summary": r["summary"],
        "strategy_used": r["strategy_used"],
        "what_worked": r["what_worked"],
        "what_failed": r["what_failed"],
        "key_citations": _arr(r["key_citations"]),
        "key_arguments": _arr(r["key_arguments"]),
        "tags": list(r["tags"] or []),
        "generated_by": r["generated_by"],
        "reviewed_by": str(r["reviewed_by"]) if r["reviewed_by"] else None,
        "reviewed_at": r["reviewed_at"].isoformat() if r["reviewed_at"] else None,
        "embedded": r["embedding_at"] is not None,
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


async def _embed_lesson(title_hint: str, lesson_text: str, tags: list[str], user_id: Optional[str]) -> Optional[str]:
    from utils.embeddings import embed_text_as_pg, compose_lesson_text
    return await embed_text_as_pg(
        compose_lesson_text(title_hint, lesson_text, tags),
        purpose="case_lesson",
        session_id=str(user_id) if user_id else "",
    )


# --------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------
@router.get("")
async def list_lessons(
    outcome: Optional[str] = Query(default=None),
    tag: Optional[str] = Query(default=None),
    matter_id: Optional[str] = Query(default=None),
    generated_by: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_current_firm),
):
    where = ["firm_id = $1::uuid"]
    args: list = [principal.firm_id]
    idx = 2
    if outcome:
        if outcome not in VALID_OUTCOMES:
            raise HTTPException(400, f"outcome inválido (válidos: {sorted(VALID_OUTCOMES)})")
        where.append(f"outcome = ${idx}"); args.append(outcome); idx += 1
    if tag:
        where.append(f"${idx} = ANY(tags)"); args.append(tag); idx += 1
    if matter_id:
        where.append(f"matter_id = ${idx}::uuid"); args.append(matter_id); idx += 1
    if generated_by:
        where.append(f"generated_by = ${idx}"); args.append(generated_by); idx += 1
    args.extend([limit, offset])
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            select id, matter_id, outcome, summary, strategy_used,
                   what_worked, what_failed, key_citations, key_arguments,
                   tags, generated_by, reviewed_by, reviewed_at,
                   embedding_at, created_at, updated_at
              from case_lessons
             where {' and '.join(where)}
             order by created_at desc
             limit ${idx} offset ${idx + 1}
            """,
            *args,
        )
    return {"items": [_serialize_lesson(r) for r in rows]}


@router.get("/matter/{matter_id}")
async def lessons_for_matter(
    matter_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, matter_id, outcome, summary, strategy_used,
                   what_worked, what_failed, key_citations, key_arguments,
                   tags, generated_by, reviewed_by, reviewed_at,
                   embedding_at, created_at, updated_at
              from case_lessons
             where firm_id = $1::uuid and matter_id = $2::uuid
             order by created_at desc
            """,
            principal.firm_id, matter_id,
        )
    return {"items": [_serialize_lesson(r) for r in rows]}


@router.get("/{lesson_id}")
async def get_lesson(
    lesson_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, matter_id, outcome, summary, strategy_used,
                   what_worked, what_failed, key_citations, key_arguments,
                   tags, generated_by, reviewed_by, reviewed_at,
                   embedding_at, created_at, updated_at
              from case_lessons
             where firm_id = $1::uuid and id = $2::uuid
            """,
            principal.firm_id, lesson_id,
        )
    if not row:
        raise HTTPException(404, "Lección no encontrada")
    return _serialize_lesson(row)


@router.post("", status_code=201)
async def create_lesson(
    body: LessonIn,
    principal: Principal = Depends(get_current_firm),
):
    if body.outcome not in VALID_OUTCOMES:
        raise HTTPException(400, f"outcome inválido (válidos: {sorted(VALID_OUTCOMES)})")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")

    title_hint = f"Lección · {body.outcome}"
    lesson_text = " · ".join(
        s for s in [body.summary, body.strategy_used, body.what_worked, body.what_failed] if s
    )
    embedding_pg = await _embed_lesson(title_hint, lesson_text, body.tags, principal.user_id)

    async with storage.pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                insert into case_lessons
                  (firm_id, matter_id, outcome, summary, strategy_used,
                   what_worked, what_failed, key_citations, key_arguments,
                   tags, generated_by, embedding, embedding_at)
                values ($1::uuid, $2::uuid, $3, $4, $5,
                        $6, $7, $8::jsonb, $9::jsonb,
                        $10, 'manual',
                        case when $11::text is not null then $11::vector else null end,
                        case when $11::text is not null then now() else null end)
                returning id, matter_id, outcome, summary, strategy_used,
                          what_worked, what_failed, key_citations, key_arguments,
                          tags, generated_by, reviewed_by, reviewed_at,
                          embedding_at, created_at, updated_at
                """,
                principal.firm_id, body.matter_id, body.outcome, body.summary,
                body.strategy_used, body.what_worked, body.what_failed,
                json.dumps(body.key_citations or []),
                json.dumps(body.key_arguments or []),
                body.tags or [], embedding_pg,
            )
        except Exception as e:
            msg = str(e).lower()
            if "unique" in msg or "duplicate" in msg:
                raise HTTPException(409, "Ya existe una lección manual para este caso (PATCH para actualizar)")
            raise HTTPException(400, f"No se pudo crear: {e}")
    return _serialize_lesson(row)


@router.patch("/{lesson_id}")
async def update_lesson(
    lesson_id: str,
    body: LessonPatch,
    principal: Principal = Depends(get_current_firm),
):
    if body.outcome and body.outcome not in VALID_OUTCOMES:
        raise HTTPException(400, "outcome inválido")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")

    sets: list[str] = []
    args: list = []
    idx = 1
    re_embed = False
    for col, val in (
        ("outcome", body.outcome), ("summary", body.summary),
        ("strategy_used", body.strategy_used),
        ("what_worked", body.what_worked), ("what_failed", body.what_failed),
    ):
        if val is not None:
            sets.append(f"{col} = ${idx}")
            args.append(val); idx += 1
            if col in ("summary", "strategy_used", "what_worked", "what_failed"):
                re_embed = True
    for col, val in (
        ("key_citations", body.key_citations),
        ("key_arguments", body.key_arguments),
    ):
        if val is not None:
            sets.append(f"{col} = ${idx}::jsonb")
            args.append(json.dumps(val)); idx += 1
    if body.tags is not None:
        sets.append(f"tags = ${idx}")
        args.append(body.tags); idx += 1
        re_embed = True
    if body.mark_reviewed:
        sets.append(f"reviewed_by = ${idx}::uuid")
        args.append(principal.user_id); idx += 1
        sets.append("reviewed_at = now()")
    if not sets:
        raise HTTPException(400, "Sin cambios")

    async with storage.pool.acquire() as conn:
        if re_embed:
            current = await conn.fetchrow(
                "select outcome, summary, strategy_used, what_worked, what_failed, tags "
                "from case_lessons where firm_id = $1::uuid and id = $2::uuid",
                principal.firm_id, lesson_id,
            )
            if not current:
                raise HTTPException(404, "Lección no encontrada")
            outcome = body.outcome or current["outcome"]
            summary = body.summary if body.summary is not None else current["summary"]
            strategy_used = body.strategy_used if body.strategy_used is not None else current["strategy_used"]
            what_worked = body.what_worked if body.what_worked is not None else current["what_worked"]
            what_failed = body.what_failed if body.what_failed is not None else current["what_failed"]
            tags = body.tags if body.tags is not None else list(current["tags"] or [])
            title_hint = f"Lección · {outcome}"
            lesson_text = " · ".join(s for s in [summary, strategy_used, what_worked, what_failed] if s)
            embedding_pg = await _embed_lesson(title_hint, lesson_text, tags, principal.user_id)
            if embedding_pg:
                sets.append(f"embedding = ${idx}::vector")
                args.append(embedding_pg); idx += 1
                sets.append("embedding_at = now()")
        args.append(principal.firm_id)
        args.append(lesson_id)
        row = await conn.fetchrow(
            f"""
            update case_lessons
               set {', '.join(sets)}
             where firm_id = ${idx}::uuid and id = ${idx + 1}::uuid
             returning id, matter_id, outcome, summary, strategy_used,
                       what_worked, what_failed, key_citations, key_arguments,
                       tags, generated_by, reviewed_by, reviewed_at,
                       embedding_at, created_at, updated_at
            """,
            *args,
        )
    if not row:
        raise HTTPException(404, "Lección no encontrada")
    return _serialize_lesson(row)


@router.delete("/{lesson_id}")
async def delete_lesson(
    lesson_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "delete from case_lessons where firm_id = $1::uuid and id = $2::uuid",
            principal.firm_id, lesson_id,
        )
    return {"deleted": True}


# --------------------------------------------------------------------
# Search semántica
# --------------------------------------------------------------------
@router.post("/search")
async def search_lessons(
    body: LessonSearchIn,
    principal: Principal = Depends(get_current_firm),
):
    if body.outcome and body.outcome not in VALID_OUTCOMES:
        raise HTTPException(400, "outcome inválido")
    from utils.db import get_storage
    from utils.embeddings import embed_text_as_pg
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    embedding_pg = await embed_text_as_pg(
        body.query,
        purpose="lessons_search",
        session_id=str(principal.user_id) if principal.user_id else "",
    )
    if not embedding_pg:
        return {"items": [], "note": "Embedding no disponible (búsqueda semántica deshabilitada)"}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select * from lexai_lessons_search($1::uuid, $2::vector, $3, $4)
            """,
            principal.firm_id, embedding_pg, body.outcome, body.limit,
        )
    return {
        "items": [
            {
                "id": str(r["id"]),
                "matter_id": str(r["matter_id"]),
                "outcome": r["outcome"],
                "summary": r["summary"],
                "strategy_used": r["strategy_used"],
                "what_worked": r["what_worked"],
                "tags": list(r["tags"] or []),
                "similarity": float(r["similarity"] or 0),
            }
            for r in rows
        ]
    }


# --------------------------------------------------------------------
# Extract · LLM lee el matter cerrado y genera una lección
# --------------------------------------------------------------------
@router.post("/extract")
async def extract_lesson(
    body: ExtractIn,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in (None, "", "admin", "socio_senior", "socio_junior", "abogado_senior", "abogado_junior", "paralegal"):
        # Permitimos casi cualquier rol; rechazamos sólo si parece "cliente"/"funcionario_publico" puros.
        if principal.role in ("cliente", "funcionario_publico"):
            raise HTTPException(403, "Sin permisos para extraer lecciones")
    from agent.tools.extract_lessons import extract_lesson_from_matter
    try:
        result = await extract_lesson_from_matter(
            firm_id=principal.firm_id,
            matter_id=body.matter_id,
            user_id=principal.user_id,
            force=body.force,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("extract_lesson failed for matter %s", body.matter_id)
        raise HTTPException(500, f"Falló la extracción: {e}")
    return result
