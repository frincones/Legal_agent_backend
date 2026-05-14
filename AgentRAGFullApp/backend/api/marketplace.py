"""Sprint 14 · Template marketplace API.

  GET    /v1/marketplace/templates                 · lista (filtros category/doc_type)
  GET    /v1/marketplace/templates/{id}            · detalle
  POST   /v1/marketplace/templates/{id}/star
  DELETE /v1/marketplace/templates/{id}/star
  POST   /v1/marketplace/templates/{id}/fork       · clona a user_templates
  POST   /v1/marketplace/templates/submit          · propone una nueva (pending_review)
  GET    /v1/marketplace/stats
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/marketplace", tags=["marketplace"])


def _extract_variables(body: str) -> list[str]:
    """{{var_name}} → ['var_name']."""
    return list(dict.fromkeys(re.findall(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}", body or "")))


@router.get("/templates")
async def list_templates(
    category: Optional[str] = None,
    doc_type: Optional[str] = None,
    is_official: Optional[bool] = None,
    q: Optional[str] = None,
    sort: str = Query(default="popular", regex="^(popular|recent|stars)$"),
    limit: int = Query(default=30, le=200),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")

    where = ["visibility = 'public'"]
    params: list = []
    if category:
        params.append(category); where.append(f"category = ${len(params)}")
    if doc_type:
        params.append(doc_type); where.append(f"doc_type = ${len(params)}")
    if is_official is not None:
        params.append(is_official); where.append(f"is_official = ${len(params)}")
    if q:
        params.append(f"%{q}%"); where.append(f"(name ilike ${len(params)} or description ilike ${len(params)})")

    order = {
        "popular": "stars desc nulls last, downloads desc",
        "recent": "created_at desc",
        "stars": "stars desc",
    }[sort]
    params.append(limit)

    sql = f"""
        select t.id, t.name, t.doc_type, t.category, t.jurisdiction,
               t.description, t.variables, t.downloads, t.stars, t.forks,
               t.is_official, t.created_at,
               exists(select 1 from template_marketplace_stars s
                       where s.item_id = t.id and s.user_id = $1::uuid) as starred_by_me
          from template_marketplace_items t
         where {' and '.join(where)}
         order by {order}
         limit ${len(params)}
    """
    p = [principal.user_id] + params
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *p)
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]), "name": r["name"], "doc_type": r["doc_type"],
                "category": r["category"], "jurisdiction": r["jurisdiction"],
                "description": r["description"],
                "variables": list(r["variables"] or []),
                "downloads": r["downloads"], "stars": r["stars"], "forks": r["forks"],
                "is_official": r["is_official"],
                "starred_by_me": r["starred_by_me"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


@router.get("/templates/{template_id}")
async def get_template(
    template_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            select id, name, doc_type, category, jurisdiction, description, body,
                   variables, downloads, stars, forks, is_official, visibility,
                   author_firm_id, created_at, updated_at,
                   exists(select 1 from template_marketplace_stars s
                           where s.item_id = template_marketplace_items.id
                             and s.user_id = $2::uuid) as starred_by_me
              from template_marketplace_items
             where id = $1::uuid
            """,
            template_id, principal.user_id,
        )
    if not r:
        raise HTTPException(404, "not found")
    if r["visibility"] != "public" and str(r["author_firm_id"]) != str(principal.firm_id):
        raise HTTPException(404, "not found")
    return {
        "id": str(r["id"]), "name": r["name"], "doc_type": r["doc_type"],
        "category": r["category"], "jurisdiction": r["jurisdiction"],
        "description": r["description"], "body": r["body"],
        "variables": list(r["variables"] or []),
        "downloads": r["downloads"], "stars": r["stars"], "forks": r["forks"],
        "is_official": r["is_official"], "visibility": r["visibility"],
        "starred_by_me": r["starred_by_me"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


@router.post("/templates/{template_id}/star")
async def star(
    template_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        inserted = await conn.fetchval(
            """
            insert into template_marketplace_stars (item_id, user_id, firm_id)
            values ($1::uuid, $2::uuid, $3::uuid)
            on conflict do nothing
            returning 1
            """,
            template_id, principal.user_id, principal.firm_id,
        )
        if inserted:
            await conn.execute(
                "update template_marketplace_items set stars = stars + 1 where id = $1::uuid",
                template_id,
            )
    return {"starred": True}


@router.delete("/templates/{template_id}/star")
async def unstar(
    template_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        n = await conn.execute(
            "delete from template_marketplace_stars where item_id = $1::uuid and user_id = $2::uuid",
            template_id, principal.user_id,
        )
        # n es 'DELETE k' string; si fue 0, no decrementamos
        if n != "DELETE 0":
            await conn.execute(
                "update template_marketplace_items set stars = greatest(stars - 1, 0) where id = $1::uuid",
                template_id,
            )
    return {"starred": False}


@router.post("/templates/{template_id}/fork")
async def fork(
    template_id: str,
    principal: Principal = Depends(get_current_firm),
):
    """Clona la plantilla a user_templates de la firma."""
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        t = await conn.fetchrow(
            """
            select name, doc_type, body, variables, jurisdiction
              from template_marketplace_items
             where id = $1::uuid and visibility = 'public'
            """,
            template_id,
        )
        if not t:
            raise HTTPException(404, "not found")
        # Insertar en user_templates (Sprint 4 table)
        try:
            row = await conn.fetchrow(
                """
                insert into user_templates
                  (firm_id, owner_id, name, doc_type, jurisdiction,
                   target_court, body, variables, is_default_for_type, is_personal)
                values ($1::uuid, $2::uuid, $3, $4, $5,
                        null, $6, $7::text[], false, false)
                returning id
                """,
                principal.firm_id, principal.user_id,
                f"{t['name']} (forked)", t["doc_type"], t["jurisdiction"],
                t["body"], list(t["variables"] or []),
            )
        except Exception as e:
            logger.warning("fork → user_templates failed: %s", e)
            raise HTTPException(500, f"no se pudo clonar: {e}")
        await conn.execute(
            "update template_marketplace_items set forks = forks + 1, downloads = downloads + 1 where id = $1::uuid",
            template_id,
        )
    return {"ok": True, "user_template_id": str(row["id"])}


class SubmitRequest(BaseModel):
    name: str = Field(min_length=2)
    doc_type: str = Field(min_length=2)
    category: str = Field(default="general")
    jurisdiction: str = Field(default="colombia")
    description: Optional[str] = None
    body: str = Field(min_length=20)


@router.post("/templates/submit")
async def submit(
    body: SubmitRequest,
    principal: Principal = Depends(get_current_firm),
):
    """Propone una nueva plantilla al marketplace (visibility='pending_review')."""
    from utils.db import get_storage
    storage = await get_storage()
    variables = _extract_variables(body.body)
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into template_marketplace_items
              (author_firm_id, author_user_id, name, doc_type, category, jurisdiction,
               description, body, variables, visibility, is_official)
            values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9::text[],
                    'pending_review', false)
            returning id, name, visibility, created_at
            """,
            principal.firm_id, principal.user_id, body.name, body.doc_type, body.category,
            body.jurisdiction, body.description, body.body, variables,
        )
    return {
        "id": str(row["id"]), "name": row["name"], "visibility": row["visibility"],
        "variables_detected": variables,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "note": "Tu plantilla está en revisión. La aprobamos manualmente para asegurar calidad.",
    }


@router.get("/stats")
async def stats(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select
              (select count(*) from template_marketplace_items where visibility = 'public') as public_count,
              (select count(*) from template_marketplace_items where is_official = true and visibility = 'public') as official_count,
              (select count(*) from template_marketplace_items where author_firm_id = $1::uuid) as my_submissions,
              (select count(*) from template_marketplace_stars where user_id = $2::uuid) as my_stars
            """,
            principal.firm_id, principal.user_id,
        )
    return {
        "public_count": row["public_count"] or 0,
        "official_count": row["official_count"] or 0,
        "my_submissions": row["my_submissions"] or 0,
        "my_stars": row["my_stars"] or 0,
    }
