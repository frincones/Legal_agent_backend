"""Sprint 7 · Global Search API.

Búsqueda híbrida (FTS + ILIKE) sobre matters + clients + documents.
Para semántica más profunda usa pgvector vía existing `chunks` (RAG).

  GET /v1/search?q=...&kinds=matter,client,document&limit=30
  GET /v1/search/quick?q=...      (top 5 por kind, para command palette)

Implementación:
  · RPC `lexai_global_search` (creada en migration 2026_05_10_sprint7)
  · El endpoint /quick agrupa por kind y devuelve top-N por categoría
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/search", tags=["search"])


@router.get("")
async def search(
    q: str = Query(min_length=2, max_length=200),
    kinds: Optional[str] = Query(default=None, description="comma-separated: matter,client,document"),
    limit: int = Query(default=30, le=200),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    selected = set((kinds or "matter,client,document").split(","))
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            "select * from lexai_global_search($1::uuid, $2, $3::int)",
            principal.firm_id, q, limit,
        )
    items = [
        {
            "kind": r["kind"],
            "id": str(r["id"]),
            "title": r["title"],
            "snippet": r["snippet"],
            "matter_id": str(r["matter_id"]) if r["matter_id"] else None,
            "client_id": str(r["client_id"]) if r["client_id"] else None,
            "rank": float(r["rank"]) if r["rank"] is not None else 0,
        }
        for r in rows
        if r["kind"] in selected
    ]
    return {"q": q, "count": len(items), "items": items}


@router.get("/quick")
async def search_quick(
    q: str = Query(min_length=2, max_length=200),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            "select * from lexai_global_search($1::uuid, $2, 30)",
            principal.firm_id, q,
        )
    grouped: dict[str, list] = {"matter": [], "client": [], "document": []}
    for r in rows:
        kind = r["kind"]
        if kind in grouped and len(grouped[kind]) < 5:
            grouped[kind].append({
                "id": str(r["id"]),
                "title": r["title"],
                "snippet": r["snippet"],
                "matter_id": str(r["matter_id"]) if r["matter_id"] else None,
                "client_id": str(r["client_id"]) if r["client_id"] else None,
            })
    return {"q": q, "groups": grouped}


# ════════════════════════════════════════════════════════════════════════
# Voice tool
# ════════════════════════════════════════════════════════════════════════


async def find_anything_tool(args: dict, ctx: dict) -> dict:
    """Voice tool: 'LexAI, busca <algo>'."""
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    q = (args.get("query") or args.get("q") or "").strip()
    if len(q) < 2:
        return {"error": "query muy corta"}
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            "select * from lexai_global_search($1::uuid, $2, 10)",
            firm_id, q,
        )
    return {
        "count": len(rows),
        "items": [
            {
                "kind": r["kind"],
                "title": r["title"],
                "snippet": r["snippet"],
                "id": str(r["id"]),
                "matter_id": str(r["matter_id"]) if r["matter_id"] else None,
            }
            for r in rows
        ],
    }
