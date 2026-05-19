"""Sprint 20 · Voice tools de Judge Perspective.

  · search_judge(q?, corte?, especialidad?)        → lista de jueces matching
  · simulate_judge_view(matter_id?, judge_id, document_text?)  → análisis IA
  · get_judge_stats(judge_id)                      → stats del juez

ctx esperado: firm_id, user_id, matter_id (opcional).
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


async def search_judge_tool(args: dict, ctx: dict) -> dict:
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    q = (args.get("q") or args.get("query") or args.get("name") or "").strip() or None
    corte = args.get("corte")
    especialidad = args.get("especialidad")
    limit = min(int(args.get("limit", 10)), 20)
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            "select * from lexai_judge_search($1, $2, $3, $4)",
            q, corte, especialidad, limit,
        )
    return {
        "items": [
            {
                "id": str(r["id"]),
                "full_name": r["full_name"],
                "corte": r["corte"],
                "sala": r["sala"],
                "especialidades": list(r["especialidades"] or []),
            }
            for r in rows
        ],
        "count": len(rows),
    }


async def get_judge_stats_tool(args: dict, ctx: dict) -> dict:
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    judge_id = (args.get("judge_id") or "").strip()
    if not judge_id:
        # Fallback: el primer juez del seed (la tabla matters no tiene
        # judge_id explícito · usa el juez relacionado por juzgado/tribunal
        # cuando esté seedeado, sino el primer juez disponible).
        from utils.db import get_storage
        _s = await get_storage()
        if hasattr(_s, "pool"):
            async with _s.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "select id from judges order by created_at limit 1"
                )
                if row:
                    judge_id = str(row["id"])
    if not judge_id:
        return {"error": "Necesito judge_id"}
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {}
    async with storage.pool.acquire() as conn:
        raw = await conn.fetchval("select lexai_judge_stats($1::uuid)", judge_id)
    if raw is None:
        return {"error": "Juez no encontrado"}
    return raw if not isinstance(raw, str) else json.loads(raw)
