"""Sprint 20 · Judge Simulator endpoints.

  POST /v1/judge-predictions/simulate
       body: { matter_id, judge_id, document_text?, use_cache? }
       → análisis IA estructurado

  GET  /v1/judge-predictions/matter/{matter_id}/latest?judge_id=
  GET  /v1/judge-predictions/matter/{matter_id}/history?limit=
  POST /v1/judge-predictions/{id}/review
  DELETE /v1/judge-predictions/{id}
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/judge-predictions", tags=["judge_predictions"])


class SimulateIn(BaseModel):
    matter_id: str
    judge_id: str
    document_text: Optional[str] = None
    use_cache: bool = True


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
        "judge_id": str(_opt("judge_id")) if _opt("judge_id") else None,
        "judge_name": _opt("judge_name"),
        "alignment_score": float(r["alignment_score"] or 0),
        "reception": r["reception"],
        "summary": r["summary"],
        "strengths": _json(r["strengths"]),
        "risk_factors": _json(r["risk_factors"]),
        "suggested_revisions": _json(r["suggested_revisions"]),
        "similar_decisions": _json(r["similar_decisions"]),
        "generated_at": r["generated_at"].isoformat() if r["generated_at"] else None,
        "reviewed_at": r["reviewed_at"].isoformat() if _opt("reviewed_at") else None,
    }


@router.post("/simulate", status_code=201)
async def simulate(
    body: SimulateIn,
    principal: Principal = Depends(get_current_firm),
):
    from agent.tools.judge_simulator import simulate_judge_view_for_matter
    try:
        result = await simulate_judge_view_for_matter(
            firm_id=principal.firm_id,
            matter_id=body.matter_id,
            judge_id=body.judge_id,
            document_text=body.document_text,
            user_id=principal.user_id,
            use_cache=body.use_cache,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("judge simulate failed")
        raise HTTPException(500, f"Fallo: {e}")
    return result


@router.get("/matter/{matter_id}/latest")
async def latest_for_matter(
    matter_id: str,
    judge_id: Optional[str] = Query(default=None),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return None
    async with storage.pool.acquire() as conn:
        if judge_id:
            row = await conn.fetchrow(
                """
                select jp.*, j.full_name as judge_name
                  from judge_predictions jp
                  join judges j on j.id = jp.judge_id
                 where jp.firm_id = $1::uuid and jp.matter_id = $2::uuid
                   and jp.judge_id = $3::uuid
                 order by jp.generated_at desc limit 1
                """,
                principal.firm_id, matter_id, judge_id,
            )
        else:
            row = await conn.fetchrow(
                """
                select jp.*, j.full_name as judge_name
                  from judge_predictions jp
                  join judges j on j.id = jp.judge_id
                 where jp.firm_id = $1::uuid and jp.matter_id = $2::uuid
                 order by jp.generated_at desc limit 1
                """,
                principal.firm_id, matter_id,
            )
    return _serialize(row) if row else None


@router.get("/matter/{matter_id}/history")
async def history_for_matter(
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
            select jp.*, j.full_name as judge_name
              from judge_predictions jp
              join judges j on j.id = jp.judge_id
             where jp.firm_id = $1::uuid and jp.matter_id = $2::uuid
             order by jp.generated_at desc
             limit $3
            """,
            principal.firm_id, matter_id, limit,
        )
    return {"items": [_serialize(r) for r in rows]}


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
            update judge_predictions
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
async def delete_prediction(
    prediction_id: str,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior", "socio_junior"):
        raise HTTPException(403, "Solo socios/admin")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "delete from judge_predictions where firm_id = $1::uuid and id = $2::uuid",
            principal.firm_id, prediction_id,
        )
    return {"deleted": True}
