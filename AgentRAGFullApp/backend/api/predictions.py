"""Sprint 17 · Predictions API.

Endpoints:
  GET    /v1/predictions/matter/{matter_id}/latest
  GET    /v1/predictions/matter/{matter_id}/history
  POST   /v1/predictions/matter/{matter_id}/generate     (re-corre LLM)
  POST   /v1/predictions/{id}/review                     (marca como revisada)
  DELETE /v1/predictions/{id}
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/predictions", tags=["predictions"])


def _serialize(r) -> dict:
    keys = set(r.keys()) if hasattr(r, "keys") else set()
    def _opt(k):
        return r[k] if k in keys else None
    def _json(v):
        if v is None:
            return []
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return []
        return v
    return {
        "id": str(r["id"]),
        "matter_id": str(_opt("matter_id")) if _opt("matter_id") else None,
        "prob_won": float(r["prob_won"] or 0),
        "prob_lost": float(r["prob_lost"] or 0),
        "prob_settled": float(r["prob_settled"] or 0),
        "prob_abandoned": float(r["prob_abandoned"] or 0),
        "confidence": float(r["confidence"] or 0),
        "primary_outcome": r["primary_outcome"],
        "summary": r["summary"],
        "recommended_strategy": r["recommended_strategy"],
        "risks": _json(r["risks"]),
        "similar_lessons": _json(r["similar_lessons"]),
        "generated_at": r["generated_at"].isoformat() if r["generated_at"] else None,
        "generated_by": _opt("generated_by"),
        "reviewed_at": r["reviewed_at"].isoformat() if r["reviewed_at"] else None,
    }


@router.get("/matter/{matter_id}/latest")
async def latest(
    matter_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return None
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            "select * from lexai_latest_prediction($1::uuid, $2::uuid)",
            principal.firm_id, matter_id,
        )
    if not row:
        return None
    return _serialize(row)


@router.get("/matter/{matter_id}/history")
async def history(
    matter_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, matter_id, prob_won, prob_lost, prob_settled, prob_abandoned,
                   confidence, primary_outcome, summary, recommended_strategy,
                   risks, similar_lessons, generated_at, generated_by, reviewed_at
              from case_predictions
             where firm_id = $1::uuid and matter_id = $2::uuid
             order by generated_at desc
             limit $3
            """,
            principal.firm_id, matter_id, limit,
        )
    return {"items": [_serialize(r) for r in rows]}


@router.post("/matter/{matter_id}/generate", status_code=201)
async def generate(
    matter_id: str,
    principal: Principal = Depends(get_current_firm),
):
    """Corre el LLM y persiste una nueva predicción."""
    from agent.tools.predict_outcome import predict_outcome_for_matter
    try:
        result = await predict_outcome_for_matter(
            firm_id=principal.firm_id,
            matter_id=matter_id,
            user_id=principal.user_id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("generate prediction failed for matter %s", matter_id)
        raise HTTPException(500, f"Fallo: {e}")
    return result


@router.post("/{prediction_id}/review")
async def review(
    prediction_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update case_predictions
               set reviewed_by = $1::uuid, reviewed_at = now()
             where firm_id = $2::uuid and id = $3::uuid
             returning id
            """,
            principal.user_id, principal.firm_id, prediction_id,
        )
    if not row:
        raise HTTPException(404, "Predicción no encontrada")
    return {"reviewed": True, "id": str(row["id"])}


@router.delete("/{prediction_id}")
async def delete(
    prediction_id: str,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior", "socio_junior"):
        raise HTTPException(403, "Solo socios/admin pueden borrar predicciones")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "delete from case_predictions where firm_id = $1::uuid and id = $2::uuid",
            principal.firm_id, prediction_id,
        )
    return {"deleted": True}
