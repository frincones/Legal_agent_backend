"""Legal Alerts API · proactive feed of changes that affect the firm.

Sprint 3 · S3-06: storage + read API. The actual watcher (background
job that scans Diario Oficial / Corte Constitucional / Senado) is
implemented in Sprint 12 (M24 Legislative Watch).

Endpoints:
  GET   /v1/legal-alerts/                → list alerts (newest first)
  GET   /v1/legal-alerts/unread-count    → badge count
  POST  /v1/legal-alerts/{id}/read       → mark single as read
  POST  /v1/legal-alerts/{id}/dismiss    → dismiss without dismissing topic
  POST  /v1/legal-alerts/read-all        → mark all unread as read
  POST  /v1/legal-alerts/                → manual create (admin/socio)
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/legal-alerts", tags=["legal_alerts"])


ALLOWED_TARGET_TYPES = {"citation", "norma", "codigo", "tema", "articulo"}
ALLOWED_KINDS = {
    "derogada", "modificada", "nueva_jurisprudencia",
    "cambio_normativo", "sentencia_relevante", "suspendida",
}
ALLOWED_SEVERITIES = {"info", "warning", "critical"}
ALLOWED_SOURCES = {
    "rules", "grafo_derogacion", "watcher_diario_oficial",
    "watcher_corte_constitucional", "watcher_senado", "manual",
}


class LegalAlertRow(BaseModel):
    id: str
    target_type: str
    target_ref: str
    kind: str
    severity: str
    title: str
    description: Optional[str]
    source_url: Optional[str]
    source: str
    affected_matter_ids: list[str] = Field(default_factory=list)
    affected_document_ids: list[str] = Field(default_factory=list)
    detected_at: datetime
    read_at: Optional[datetime]
    dismissed_at: Optional[datetime]


class LegalAlertCreate(BaseModel):
    target_type: str
    target_ref: str = Field(..., min_length=2, max_length=120)
    kind: str
    severity: str = "info"
    title: str = Field(..., min_length=4, max_length=200)
    description: Optional[str] = Field(None, max_length=1500)
    source_url: Optional[str] = None
    source: str = "manual"
    affected_matter_ids: list[str] = Field(default_factory=list)
    affected_document_ids: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


def _row(r) -> LegalAlertRow:
    return LegalAlertRow(
        id=str(r["id"]),
        target_type=r["target_type"],
        target_ref=r["target_ref"],
        kind=r["kind"],
        severity=r["severity"],
        title=r["title"],
        description=r["description"],
        source_url=r["source_url"],
        source=r["source"],
        affected_matter_ids=[str(x) for x in (r["affected_matter_ids"] or [])],
        affected_document_ids=[str(x) for x in (r["affected_document_ids"] or [])],
        detected_at=r["detected_at"],
        read_at=r["read_at"],
        dismissed_at=r["dismissed_at"],
    )


# ────────────────────────────────────────────────────────────────────
# Read endpoints
# ────────────────────────────────────────────────────────────────────


@router.get("/", response_model=list[LegalAlertRow])
async def list_alerts(
    principal: Principal = Depends(get_current_firm),
    limit: int = Query(default=50, le=200),
    only_unread: bool = False,
    severity: Optional[str] = None,
):
    where = ["firm_id = $1::uuid", "dismissed_at is null"]
    params: list = [principal.firm_id]
    if only_unread:
        where.append("read_at is null")
    if severity:
        params.append(severity)
        where.append(f"severity = ${len(params)}")
    params.append(limit)
    sql = f"""
        select id, target_type, target_ref, kind, severity, title, description,
               source_url, source, affected_matter_ids, affected_document_ids,
               detected_at, read_at, dismissed_at
        from legal_alerts
        where {' and '.join(where)}
        order by detected_at desc
        limit ${len(params)}
    """
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [_row(r) for r in rows]


@router.get("/unread-count")
async def unread_count(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        n = await conn.fetchval(
            """
            select count(*) from legal_alerts
            where firm_id = $1::uuid
              and read_at is null
              and dismissed_at is null
            """,
            principal.firm_id,
        )
    return {"count": int(n or 0)}


# ────────────────────────────────────────────────────────────────────
# Mutations
# ────────────────────────────────────────────────────────────────────


@router.post("/{alert_id}/read", status_code=204)
async def mark_read(alert_id: str, principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        result = await conn.execute(
            """
            update legal_alerts set read_at = now()
            where id = $1::uuid and firm_id = $2::uuid
              and read_at is null
            """,
            alert_id, principal.firm_id,
        )
    if result.endswith(" 0"):
        raise HTTPException(404, "alert not found")


@router.post("/{alert_id}/dismiss", status_code=204)
async def dismiss(alert_id: str, principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        result = await conn.execute(
            """
            update legal_alerts set dismissed_at = now(),
                                    read_at = coalesce(read_at, now())
            where id = $1::uuid and firm_id = $2::uuid
            """,
            alert_id, principal.firm_id,
        )
    if result.endswith(" 0"):
        raise HTTPException(404, "alert not found")


@router.post("/read-all", status_code=204)
async def mark_all_read(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            update legal_alerts set read_at = now()
            where firm_id = $1::uuid and read_at is null and dismissed_at is null
            """,
            principal.firm_id,
        )


@router.post("/", response_model=LegalAlertRow, status_code=201)
async def create_alert(
    body: LegalAlertCreate,
    principal: Principal = Depends(get_current_firm),
):
    if body.target_type not in ALLOWED_TARGET_TYPES:
        raise HTTPException(400, f"target_type inválido: {body.target_type}")
    if body.kind not in ALLOWED_KINDS:
        raise HTTPException(400, f"kind inválido: {body.kind}")
    if body.severity not in ALLOWED_SEVERITIES:
        raise HTTPException(400, f"severity inválida: {body.severity}")
    if body.source not in ALLOWED_SOURCES:
        raise HTTPException(400, f"source inválido: {body.source}")
    aid = str(uuid.uuid4())
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into legal_alerts (
              id, firm_id, user_id, target_type, target_ref, kind, severity,
              title, description, source_url, source,
              affected_matter_ids, affected_document_ids, metadata
            )
            values (
              $1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7,
              $8, $9, $10, $11,
              $12::uuid[], $13::uuid[], $14::jsonb
            )
            returning id, target_type, target_ref, kind, severity, title, description,
                      source_url, source, affected_matter_ids, affected_document_ids,
                      detected_at, read_at, dismissed_at
            """,
            aid, principal.firm_id, principal.user_id,
            body.target_type, body.target_ref, body.kind, body.severity,
            body.title, body.description, body.source_url, body.source,
            body.affected_matter_ids, body.affected_document_ids,
            json.dumps(body.metadata),
        )
    return _row(row)
