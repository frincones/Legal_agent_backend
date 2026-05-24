"""Sprint M15 · Admin endpoints para métricas de verificación."""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from utils.db import get_storage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/admin/citations", tags=["admin-citations"])


async def _require_admin(request: Request) -> dict:
    """Stub guard — usa el bearer token + dejar que el backend valide.
    En producción real, validar contra is_admin del firm."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing_bearer_token")
    return {"token": auth[7:]}


@router.get("/health")
async def citations_health(_claims: dict = Depends(_require_admin)):
    """Métricas de verificación de los últimos 7 días."""
    storage = await get_storage()
    try:
        async with storage.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM v_verification_health LIMIT 100")
        out = []
        for r in rows:
            out.append({
                "result_state": r["result_state"],
                "source": r["source"],
                "total": r["total"],
                "avg_confidence": float(r["avg_confidence"]) if r["avg_confidence"] else 0,
                "avg_duration_ms": float(r["avg_duration_ms"]) if r["avg_duration_ms"] else 0,
                "p50_ms": float(r["p50_ms"]) if r["p50_ms"] else 0,
                "p95_ms": float(r["p95_ms"]) if r["p95_ms"] else 0,
                "verificadas": r["verificadas"],
                "no_encontradas": r["no_encontradas"],
                "last_attempt_at": r["last_attempt_at"].isoformat() if r["last_attempt_at"] else None,
            })
        return {"period": "7d", "metrics": out, "total_rows": len(out)}
    except Exception as e:
        logger.warning("citations_health failed: %s", e)
        return {"period": "7d", "metrics": [], "error": str(e)[:200]}


@router.get("/recent-failures")
async def recent_failures(limit: int = 20, _claims: dict = Depends(_require_admin)):
    """Últimas N citas marcadas como no_encontrada."""
    storage = await get_storage()
    try:
        async with storage.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT citation_ref, ref_type, result_state, source,
                       confidence_score, normalized_ref, duration_ms,
                       sources_tried, created_at
                FROM verification_attempts
                WHERE result_state IN ('no_encontrada', 'error', 'sospechosa')
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )
        return {
            "failures": [
                {
                    "citation_ref": r["citation_ref"],
                    "ref_type": r["ref_type"],
                    "result_state": r["result_state"],
                    "source": r["source"],
                    "confidence_score": float(r["confidence_score"]) if r["confidence_score"] else 0,
                    "normalized_ref": r["normalized_ref"],
                    "duration_ms": r["duration_ms"],
                    "sources_tried": r["sources_tried"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in rows
            ],
            "total": len(rows),
        }
    except Exception as e:
        logger.warning("recent_failures failed: %s", e)
        return {"failures": [], "error": str(e)[:200]}


@router.get("/shadow-diffs")
async def shadow_diffs(critical_only: bool = False, limit: int = 50,
                       _claims: dict = Depends(_require_admin)):
    """Divergencias del SHADOW_MODE entre legacy y nuevo agent."""
    storage = await get_storage()
    try:
        where = "WHERE is_critical = true" if critical_only else ""
        async with storage.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT citation_ref, citation_type, legacy_state, legacy_method,
                       agent_state, agent_method, agent_confidence,
                       is_critical, diff_type, created_at
                FROM verification_shadow_diffs
                {where}
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )
        # Summary
        summary_rows = await conn.fetch("SELECT * FROM v_verification_shadow_summary")
        summary = [
            {
                "diff_type": s["diff_type"],
                "total": s["total"],
                "critical_count": s["critical_count"],
                "unique_citations": s["unique_citations"],
            }
            for s in summary_rows
        ]
        return {
            "summary": summary,
            "diffs": [
                {
                    "citation_ref": r["citation_ref"],
                    "citation_type": r["citation_type"],
                    "legacy_state": r["legacy_state"],
                    "legacy_method": r["legacy_method"],
                    "agent_state": r["agent_state"],
                    "agent_method": r["agent_method"],
                    "agent_confidence": float(r["agent_confidence"]) if r["agent_confidence"] else 0,
                    "is_critical": r["is_critical"],
                    "diff_type": r["diff_type"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in rows
            ],
        }
    except Exception as e:
        logger.warning("shadow_diffs failed: %s", e)
        return {"summary": [], "diffs": [], "error": str(e)[:200]}
