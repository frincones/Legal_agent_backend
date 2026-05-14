"""Sprint 21 · Evidence Authenticity Checker API.

Endpoints:
  POST /v1/evidence/validate-identity        · cross-check Registro Civil/RUE/RUT
  GET  /v1/evidence/validations/latest?subject_id_value=&matter_document_id=
  GET  /v1/evidence/validations/matter/{matter_id}/history

  POST /v1/evidence/detect-inconsistencies   · LLM analiza doc
  GET  /v1/evidence/inconsistencies/document/{matter_document_id}/latest
  GET  /v1/evidence/inconsistencies/document/{matter_document_id}/history

  POST /v1/evidence/score                    · combina validation + inconsistencies + features
  GET  /v1/evidence/scores/document/{matter_document_id}/latest
  GET  /v1/evidence/scores/matter/{matter_id}/history
  POST /v1/evidence/scores/{id}/review

  GET  /v1/evidence/matter/{matter_id}/stats
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/evidence", tags=["evidence"])


VALID_SUBJECT_KINDS = {"persona", "empresa"}
VALID_ID_KINDS = {"cedula", "nit", "pasaporte", "rut", "otro"}


# --------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------
class ValidateIdentityIn(BaseModel):
    subject_kind: str = Field(default="persona")
    subject_id_kind: str = Field(default="cedula")
    subject_id_value: str
    subject_name: Optional[str] = None
    matter_id: Optional[str] = None
    matter_document_id: Optional[str] = None


class DetectInconsistenciesIn(BaseModel):
    matter_document_id: str
    document_text: str
    matter_id: Optional[str] = None
    use_cache: bool = True


class ScoreIn(BaseModel):
    matter_document_id: str
    document_text: str
    matter_id: Optional[str] = None
    mime_type: Optional[str] = None
    auto_run_validation: bool = False
    auto_run_inconsistencies: bool = True
    subject_kind: Optional[str] = None
    subject_id_kind: Optional[str] = None
    subject_id_value: Optional[str] = None
    subject_name: Optional[str] = None


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------
def _parse_jsonb(v):
    if v is None:
        return [] if not isinstance(v, dict) else {}
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return []
    return v


def _serialize_validation(r) -> dict:
    return {
        "id": str(r["id"]),
        "subject_kind": r["subject_kind"],
        "subject_id_kind": r["subject_id_kind"],
        "subject_id_value": r["subject_id_value"],
        "subject_name": r["subject_name"],
        "status": r["status"],
        "providers_used": list(r["providers_used"] or []),
        "results": _parse_jsonb(r["results"]),
        "mismatches": _parse_jsonb(r["mismatches"]),
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


def _serialize_inconsistency(r) -> dict:
    return {
        "id": str(r["id"]),
        "matter_document_id": str(r["matter_document_id"]) if r.get("matter_document_id") else None,
        "inconsistencies": _parse_jsonb(r["inconsistencies"]),
        "total_count": int(r["total_count"] or 0),
        "high_severity_count": int(r["high_severity_count"] or 0),
        "summary": r["summary"],
        "analyzed_at": r["analyzed_at"].isoformat() if r["analyzed_at"] else None,
    }


def _serialize_score(r) -> dict:
    return {
        "id": str(r["id"]),
        "probative_score": int(r["probative_score"] or 0),
        "level": r["level"],
        "summary": r["summary"],
        "positive_factors": _parse_jsonb(r["positive_factors"]),
        "negative_factors": _parse_jsonb(r["negative_factors"]),
        "recommendations": _parse_jsonb(r["recommendations"]),
        "validation_id": str(r["validation_id"]) if r.get("validation_id") else None,
        "inconsistency_id": str(r["inconsistency_id"]) if r.get("inconsistency_id") else None,
        "computed_at": r["computed_at"].isoformat() if r["computed_at"] else None,
        "reviewed_at": r["reviewed_at"].isoformat() if r.get("reviewed_at") else None,
    }


# --------------------------------------------------------------------
# Validation endpoints
# --------------------------------------------------------------------
@router.post("/validate-identity", status_code=201)
async def validate_identity(
    body: ValidateIdentityIn,
    principal: Principal = Depends(get_current_firm),
):
    if body.subject_kind not in VALID_SUBJECT_KINDS:
        raise HTTPException(400, f"subject_kind inválido (válidos: {sorted(VALID_SUBJECT_KINDS)})")
    if body.subject_id_kind not in VALID_ID_KINDS:
        raise HTTPException(400, f"subject_id_kind inválido (válidos: {sorted(VALID_ID_KINDS)})")
    if not body.subject_id_value or not body.subject_id_value.strip():
        raise HTTPException(400, "subject_id_value requerido")
    from agent.tools.evidence_validator import run_validation
    try:
        return await run_validation(
            firm_id=principal.firm_id,
            subject_kind=body.subject_kind,
            subject_id_kind=body.subject_id_kind,
            subject_id_value=body.subject_id_value.strip(),
            subject_name=body.subject_name,
            matter_id=body.matter_id,
            matter_document_id=body.matter_document_id,
            user_id=principal.user_id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("validate_identity failed")
        raise HTTPException(500, f"Fallo: {e}")


@router.get("/validations/latest")
async def validations_latest(
    matter_document_id: Optional[str] = Query(default=None),
    subject_id_value: Optional[str] = Query(default=None),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return None
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            "select * from lexai_latest_identity_validation($1::uuid, $2::uuid, $3)",
            principal.firm_id, matter_document_id, subject_id_value,
        )
    return _serialize_validation(row) if row else None


@router.get("/validations/matter/{matter_id}/history")
async def validations_history(
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
            select id, subject_kind, subject_id_kind, subject_id_value, subject_name,
                   status, providers_used, results, mismatches, created_at
              from evidence_validations
             where firm_id = $1::uuid and matter_id = $2::uuid
             order by created_at desc
             limit $3
            """,
            principal.firm_id, matter_id, limit,
        )
    return {"items": [_serialize_validation(r) for r in rows]}


# --------------------------------------------------------------------
# Inconsistencies endpoints
# --------------------------------------------------------------------
@router.post("/detect-inconsistencies", status_code=201)
async def detect_inconsistencies(
    body: DetectInconsistenciesIn,
    principal: Principal = Depends(get_current_firm),
):
    if not body.document_text or len(body.document_text.strip()) < 100:
        raise HTTPException(400, "document_text muy corto (mín 100 caracteres)")
    from agent.tools.inconsistency_detector import detect_inconsistencies_in_document
    try:
        return await detect_inconsistencies_in_document(
            firm_id=principal.firm_id,
            matter_document_id=body.matter_document_id,
            document_text=body.document_text,
            matter_id=body.matter_id,
            user_id=principal.user_id,
            use_cache=body.use_cache,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("detect_inconsistencies failed")
        raise HTTPException(500, f"Fallo: {e}")


@router.get("/inconsistencies/document/{matter_document_id}/latest")
async def inconsistencies_latest(
    matter_document_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return None
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            "select * from lexai_latest_inconsistencies($1::uuid, $2::uuid)",
            principal.firm_id, matter_document_id,
        )
    return _serialize_inconsistency(row) if row else None


@router.get("/inconsistencies/document/{matter_document_id}/history")
async def inconsistencies_history(
    matter_document_id: str,
    limit: int = Query(default=10, ge=1, le=50),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, matter_document_id, inconsistencies, total_count,
                   high_severity_count, summary, analyzed_at
              from evidence_inconsistencies
             where firm_id = $1::uuid and matter_document_id = $2::uuid
             order by analyzed_at desc
             limit $3
            """,
            principal.firm_id, matter_document_id, limit,
        )
    return {"items": [_serialize_inconsistency(r) for r in rows]}


# --------------------------------------------------------------------
# Probative score endpoints
# --------------------------------------------------------------------
@router.post("/score", status_code=201)
async def compute_score(
    body: ScoreIn,
    principal: Principal = Depends(get_current_firm),
):
    if not body.document_text or len(body.document_text.strip()) < 50:
        raise HTTPException(400, "document_text requerido (mín 50 caracteres)")
    from agent.tools.probative_scorer import compute_probative_score
    try:
        return await compute_probative_score(
            firm_id=principal.firm_id,
            matter_document_id=body.matter_document_id,
            document_text=body.document_text,
            matter_id=body.matter_id,
            user_id=principal.user_id,
            mime_type=body.mime_type,
            auto_run_validation=body.auto_run_validation,
            auto_run_inconsistencies=body.auto_run_inconsistencies,
            subject_kind=body.subject_kind,
            subject_id_kind=body.subject_id_kind,
            subject_id_value=body.subject_id_value,
            subject_name=body.subject_name,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("compute_score failed")
        raise HTTPException(500, f"Fallo: {e}")


@router.get("/scores/document/{matter_document_id}/latest")
async def score_latest(
    matter_document_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return None
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            "select * from lexai_latest_probative_score($1::uuid, $2::uuid)",
            principal.firm_id, matter_document_id,
        )
    return _serialize_score(row) if row else None


@router.get("/scores/matter/{matter_id}/history")
async def score_history(
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
            select id, probative_score, level, summary, positive_factors,
                   negative_factors, recommendations, validation_id,
                   inconsistency_id, computed_at, reviewed_at
              from evidence_scores
             where firm_id = $1::uuid and matter_id = $2::uuid
             order by computed_at desc
             limit $3
            """,
            principal.firm_id, matter_id, limit,
        )
    return {"items": [_serialize_score(r) for r in rows]}


@router.post("/scores/{score_id}/review")
async def review_score(
    score_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update evidence_scores
               set reviewed_by = $1::uuid, reviewed_at = now()
             where firm_id = $2::uuid and id = $3::uuid
             returning id
            """,
            principal.user_id, principal.firm_id, score_id,
        )
    if not row:
        raise HTTPException(404, "Score no encontrado")
    return {"reviewed": True, "id": str(row["id"])}


# --------------------------------------------------------------------
# Stats por matter
# --------------------------------------------------------------------
@router.get("/matter/{matter_id}/stats")
async def matter_evidence_stats(
    matter_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {}
    async with storage.pool.acquire() as conn:
        raw = await conn.fetchval(
            "select lexai_evidence_stats($1::uuid, $2::uuid)",
            principal.firm_id, matter_id,
        )
    if raw is None:
        return {}
    return raw if not isinstance(raw, str) else json.loads(raw)
