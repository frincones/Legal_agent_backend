"""Sprint 20 · Judges API.

Endpoints:
  GET  /v1/judges                  · list + filters (q, corte, especialidad)
  GET  /v1/judges/{id}             · detail + perfil + stats
  GET  /v1/judges/{id}/stats       · RPC lexai_judge_stats
  GET  /v1/judges/{id}/decisions   · decisiones desde jurisprudencia
  GET  /v1/judges/cortes           · catálogo de cortes (para filtros UI)

Los datos de jueces son SHARED cross-firm (data legal pública). Cualquier
usuario autenticado puede leer. Solo service_role escribe (gestión central).
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/judges", tags=["judges"])


VALID_CORTES = {
    "CORTE_CONSTITUCIONAL", "CORTE_SUPREMA", "CONSEJO_ESTADO",
    "TRIBUNAL_SUPERIOR", "JUZGADO_CIRCUITO", "JUZGADO_MUNICIPAL", "OTRO",
}


def _serialize_judge_row(r) -> dict:
    keys = set(r.keys()) if hasattr(r, "keys") else set()
    def _opt(k):
        return r[k] if k in keys else None
    return {
        "id": str(r["id"]),
        "full_name": r["full_name"],
        "name_variants": list(_opt("name_variants") or []),
        "corte": r["corte"],
        "sala": _opt("sala"),
        "cargo": _opt("cargo"),
        "ciudad": _opt("ciudad"),
        "especialidades": list(_opt("especialidades") or []),
        "perfil": _opt("perfil"),
        "decisions_total": int(_opt("decisions_total") or 0),
        "decisions_won_pct": float(_opt("decisions_won_pct") or 0) if _opt("decisions_won_pct") is not None else None,
        "decisions_lost_pct": float(_opt("decisions_lost_pct") or 0) if _opt("decisions_lost_pct") is not None else None,
        "decisions_settled_pct": float(_opt("decisions_settled_pct") or 0) if _opt("decisions_settled_pct") is not None else None,
        "source_url": _opt("source_url"),
        "active": bool(_opt("active") if _opt("active") is not None else True),
        "rank": float(_opt("rank") or 0) if _opt("rank") is not None else None,
    }


@router.get("/cortes")
async def list_cortes(_: Principal = Depends(get_current_firm)):
    """Devuelve catálogo de cortes con counts · útil para filtros UI."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select corte, count(*)::int as count
              from judges
             where active = true
             group by corte
             order by corte
            """,
        )
    labels = {
        "CORTE_CONSTITUCIONAL": "Corte Constitucional",
        "CORTE_SUPREMA": "Corte Suprema de Justicia",
        "CONSEJO_ESTADO": "Consejo de Estado",
        "TRIBUNAL_SUPERIOR": "Tribunal Superior",
        "JUZGADO_CIRCUITO": "Juzgado de Circuito",
        "JUZGADO_MUNICIPAL": "Juzgado Municipal",
        "OTRO": "Otros",
    }
    return {
        "items": [
            {"code": r["corte"], "label": labels.get(r["corte"], r["corte"]), "count": r["count"]}
            for r in rows
        ]
    }


@router.get("")
async def list_judges(
    q: Optional[str] = Query(default=None, max_length=200),
    corte: Optional[str] = Query(default=None),
    especialidad: Optional[str] = Query(default=None, max_length=80),
    limit: int = Query(default=20, ge=1, le=100),
    principal: Principal = Depends(get_current_firm),
):
    if corte and corte not in VALID_CORTES:
        raise HTTPException(400, f"corte inválido (válidos: {sorted(VALID_CORTES)})")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            "select * from lexai_judge_search($1, $2, $3, $4)",
            (q or None), (corte or None), (especialidad or None), limit,
        )
    return {"items": [_serialize_judge_row(r) for r in rows], "count": len(rows)}


@router.get("/{judge_id}")
async def get_judge(
    judge_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, full_name, name_variants, corte, sala, cargo, ciudad,
                   especialidades, perfil, decisions_total, decisions_won_pct,
                   decisions_lost_pct, decisions_settled_pct,
                   source_url, active, created_at, updated_at
              from judges
             where id = $1::uuid
            """,
            judge_id,
        )
    if not row:
        raise HTTPException(404, "Juez no encontrado")
    return _serialize_judge_row(row)


@router.get("/{judge_id}/stats")
async def judge_stats(
    judge_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {}
    async with storage.pool.acquire() as conn:
        raw = await conn.fetchval(
            "select lexai_judge_stats($1::uuid)", judge_id,
        )
    if raw is None:
        raise HTTPException(404, "Juez no encontrado")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return raw


@router.get("/{judge_id}/decisions")
async def judge_decisions(
    judge_id: str,
    limit: int = Query(default=10, ge=1, le=50),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            "select * from lexai_judge_decisions($1::uuid, $2)",
            judge_id, limit,
        )
    return {
        "items": [
            {
                "id": str(r["id"]) if r["id"] else None,
                "numero": r["numero"],
                "corte": r["corte"],
                "sala": r["sala"],
                "tipo_sentencia": r["tipo_sentencia"],
                "fecha": r["fecha"].isoformat() if r["fecha"] else None,
                "temas": list(r["temas"] or []),
                "ratio_decidendi": r["ratio_decidendi"],
                "fuente_url": r["fuente_url"],
            }
            for r in rows
        ]
    }
