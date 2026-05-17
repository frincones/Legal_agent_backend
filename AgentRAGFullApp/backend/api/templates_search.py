"""Semantic search across system + firm templates.

  POST /v1/templates/search
       body: {
         q: str,                  # natural language query
         materia?: str,           # optional filter (materia_legal enum)
         doc_type?: str,          # optional filter
         limit?: int = 10,
         include_firm?: bool = true   # search firm-owned + system, vs system only
       }
       → { results: [{template_id, name, materia, doc_type, score,
                       quality_score, snippet, is_system}, ...] }

Strategy:
  · pgvector-friendly when chunks indexed; falls back to ILIKE + scoring
    so it works immediately after the migration (before Sprint 2 batch
    ingestion populates `documents`/chunks).
  · Materia boost: if matter context is provided we bump rows that match
    the user's materia.
  · Recency / popularity tiebreaker: usage_count + last_used_at desc.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/templates", tags=["templates_search"])


class TemplateSearchIn(BaseModel):
    q: str = Field(..., min_length=1, max_length=500)
    materia: Optional[str] = None
    doc_type: Optional[str] = None
    limit: int = Field(10, ge=1, le=50)
    include_firm: bool = True


class TemplateHit(BaseModel):
    template_id: str
    name: str
    materia: Optional[str]
    doc_type: str
    subtype: Optional[str]
    jurisdiction: str
    quality_score: Optional[float]
    is_system: bool
    score: float                       # final ranked score (0-1)
    snippet: str                       # first 240 chars of content_md
    applicable_norms: list[str]


@router.post("/search", response_model=dict)
async def templates_search(
    body: TemplateSearchIn,
    principal: Principal = Depends(get_current_firm),
):
    """Returns top-N templates ranked by relevance to `q`.

    Initial implementation (Sprint 2): full-text + ILIKE + materia boost.
    Sprint 3 swaps the inner ranker to pgvector cosine when chunks for
    `doc_type='template'` are populated.
    """
    from utils.db import get_storage
    storage = await get_storage()

    q = body.q.strip()
    if not q:
        raise HTTPException(400, detail="empty query")

    # Build the WHERE clauses defensively.
    conditions: list[str] = []
    params: list = []

    # Scope: firm-owned + system, or system-only.
    if body.include_firm:
        conditions.append("(firm_id is null or firm_id = $1::uuid)")
        params.append(principal.firm_id)
    else:
        conditions.append("firm_id is null")
        # Still bind firm_id so param indices match (we'll just not use it).
        params.append(principal.firm_id)

    if body.materia:
        params.append(body.materia)
        conditions.append(f"materia = ${len(params)}::materia_legal")

    if body.doc_type:
        params.append(body.doc_type)
        conditions.append(f"doc_type = ${len(params)}")

    # Tokenize the query for naive matching (no full-text setup needed here).
    tokens = [t.strip() for t in q.lower().split() if len(t) > 2][:8]

    # Build a scoring expression: count of tokens matched in name + content,
    # weighted higher for name matches, with quality_score and materia boosts.
    name_score_parts: list[str] = []
    body_score_parts: list[str] = []
    for tok in tokens:
        params.append(f"%{tok}%")
        idx = len(params)
        name_score_parts.append(f"(case when name ilike ${idx} then 3 else 0 end)")
        body_score_parts.append(f"(case when content_md ilike ${idx} then 1 else 0 end)")

    name_score_sql = " + ".join(name_score_parts) if name_score_parts else "0"
    body_score_sql = " + ".join(body_score_parts) if body_score_parts else "0"

    materia_boost_sql = "0.0"
    # Optional context boost · if request came with materia, prefer matching templates.
    if body.materia:
        params.append(body.materia)
        idx = len(params)
        materia_boost_sql = f"(case when materia = ${idx}::materia_legal then 2 else 0 end)"

    # Quality multiplier · null treated as 0.6 baseline so unscored seeds still rank.
    quality_sql = "coalesce(quality_score, 0.6)"

    where = " and ".join(conditions) if conditions else "true"
    params.append(body.limit)
    limit_idx = len(params)

    sql = f"""
        with scored as (
          select id, firm_id, name, doc_type, jurisdiction,
                 materia::text as materia, subtype,
                 quality_score, content_md, applicable_norms,
                 (({name_score_sql}) + ({body_score_sql}) + ({materia_boost_sql})) as raw_score
            from user_templates
           where {where}
        )
        select id, firm_id, name, doc_type, jurisdiction, materia, subtype,
               quality_score, applicable_norms,
               left(content_md, 240) as snippet,
               (raw_score::float * {quality_sql}) as final_score
          from scored
         where raw_score > 0
         order by final_score desc nulls last,
                  quality_score desc nulls last,
                  name asc
         limit ${limit_idx}
    """

    try:
        async with storage.pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
    except Exception as e:
        logger.exception("templates_search query failed: %s", e)
        raise HTTPException(500, detail=f"search failed: {e}")

    hits: list[dict] = []
    # Normalize final_score into [0, 1] using the max of this result set.
    max_score = max((r["final_score"] or 0.0) for r in rows) if rows else 1.0
    if max_score <= 0:
        max_score = 1.0

    for r in rows:
        norms = r["applicable_norms"] or []
        if isinstance(norms, str):
            # pg text[] may come back as native list; defensive fallback only.
            norms = [norms]
        hits.append(
            TemplateHit(
                template_id=str(r["id"]),
                name=r["name"],
                materia=r["materia"],
                doc_type=r["doc_type"],
                subtype=r["subtype"],
                jurisdiction=r["jurisdiction"] or "CO",
                quality_score=float(r["quality_score"]) if r["quality_score"] is not None else None,
                is_system=r["firm_id"] is None,
                score=round((r["final_score"] or 0.0) / max_score, 3),
                snippet=(r["snippet"] or "").strip(),
                applicable_norms=list(norms),
            ).model_dump()
        )

    return {"q": q, "count": len(hits), "results": hits}


@router.get("/system/by-id/{template_id}")
async def get_system_template(
    template_id: str,
    principal: Principal = Depends(get_current_firm),
):
    """Return one template by id · accessible to any firm if it's system."""
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, firm_id, name, doc_type, jurisdiction,
                   materia::text as materia, subtype, content_md, variables,
                   applicable_norms, quality_score, clauses_jsonb,
                   usage_count, last_used_at
              from user_templates
             where id = $1::uuid
               and (firm_id is null or firm_id = $2::uuid)
            """,
            template_id, principal.firm_id,
        )
    if not row:
        raise HTTPException(404, detail="template not found or not accessible")
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "doc_type": row["doc_type"],
        "jurisdiction": row["jurisdiction"],
        "materia": row["materia"],
        "subtype": row["subtype"],
        "content_md": row["content_md"],
        "variables": row["variables"] or [],
        "applicable_norms": row["applicable_norms"] or [],
        "quality_score": float(row["quality_score"]) if row["quality_score"] is not None else None,
        "clauses": row["clauses_jsonb"],
        "is_system": row["firm_id"] is None,
        "usage_count": row["usage_count"],
        "last_used_at": row["last_used_at"].isoformat() if row["last_used_at"] else None,
    }
