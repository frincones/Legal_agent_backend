"""Repo CRUD para generation_audit."""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


class AuditRepo:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def insert_audit(
        self,
        generation_id: str,
        template_id: str,
        firm_id: str | None = None,
        matter_id: str | None = None,
        document_id: str | None = None,
        template_version: int = 1,
        model_used: dict[str, str] | None = None,
        duration_seconds: float = 0,
        cost_usd: float = 0,
        citations: list[dict] | None = None,
        calculations: dict | None = None,
        validation_passed: bool = False,
        qa_score: float | None = None,
        warnings: list[str] | None = None,
        audit_json: dict | None = None,
    ) -> str | None:
        """Inserta un audit record. Devuelve audit_id."""
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    INSERT INTO generation_audit
                        (generation_id, firm_id, matter_id, document_id,
                         template_id, template_version, model_used,
                         duration_seconds, cost_usd, citations, calculations,
                         validation_passed, qa_score, warnings, audit_json)
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9,
                            $10::jsonb, $11::jsonb, $12, $13, $14::jsonb, $15::jsonb)
                    ON CONFLICT (generation_id) DO UPDATE
                        SET duration_seconds = EXCLUDED.duration_seconds,
                            cost_usd = EXCLUDED.cost_usd,
                            citations = EXCLUDED.citations,
                            calculations = EXCLUDED.calculations,
                            validation_passed = EXCLUDED.validation_passed,
                            qa_score = EXCLUDED.qa_score,
                            warnings = EXCLUDED.warnings,
                            audit_json = EXCLUDED.audit_json
                    RETURNING id
                """,
                    uuid.UUID(generation_id),
                    uuid.UUID(firm_id) if firm_id else None,
                    uuid.UUID(matter_id) if matter_id else None,
                    uuid.UUID(document_id) if document_id else None,
                    template_id, template_version,
                    json.dumps(model_used or {}),
                    duration_seconds, cost_usd,
                    json.dumps(citations or [], default=str),
                    json.dumps(calculations or {}, default=str),
                    validation_passed, qa_score,
                    json.dumps(warnings or []),
                    json.dumps(audit_json or {}, default=str),
                )
            return str(row["id"]) if row else None
        except Exception as e:
            logger.warning("insert_audit failed: %s", e)
            return None

    async def get_audit_by_generation(self, generation_id: str) -> dict | None:
        """Recupera audit por generation_id."""
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT * FROM generation_audit WHERE generation_id = $1
                """, uuid.UUID(generation_id))
            if not row:
                return None
            return {
                "id": str(row["id"]),
                "generation_id": str(row["generation_id"]),
                "firm_id": str(row["firm_id"]) if row["firm_id"] else None,
                "matter_id": str(row["matter_id"]) if row["matter_id"] else None,
                "document_id": str(row["document_id"]) if row["document_id"] else None,
                "template_id": row["template_id"],
                "template_version": row["template_version"],
                "model_used": row["model_used"] if isinstance(row["model_used"], dict)
                              else json.loads(row["model_used"] or "{}"),
                "duration_seconds": float(row["duration_seconds"]) if row["duration_seconds"] else 0,
                "cost_usd": float(row["cost_usd"]) if row["cost_usd"] else 0,
                "citations": row["citations"] if isinstance(row["citations"], list)
                              else json.loads(row["citations"] or "[]"),
                "calculations": row["calculations"] if isinstance(row["calculations"], dict)
                              else json.loads(row["calculations"] or "{}"),
                "validation_passed": row["validation_passed"],
                "qa_score": float(row["qa_score"]) if row["qa_score"] else None,
                "warnings": row["warnings"] if isinstance(row["warnings"], list)
                              else json.loads(row["warnings"] or "[]"),
                "audit_json": row["audit_json"] if isinstance(row["audit_json"], dict)
                              else json.loads(row["audit_json"] or "{}"),
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            }
        except Exception as e:
            logger.warning("get_audit_by_generation failed: %s", e)
            return None
