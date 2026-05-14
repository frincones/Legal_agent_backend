"""Sprint 16 · Activity feed API.

  GET /v1/activity?matter_id=&actor_id=&kinds=&since=&limit=

Devuelve el feed unificado (`activity_events`) con join a `users` para
mostrar el actor. La tabla es poblada por:
  - triggers SQL (comments_added/resolved/edited)
  - llamadas explícitas desde otros routers (doc_uploaded, lesson_extracted, etc.)

Para que no se haga ruidoso, el sprint sólo activa triggers para comments.
Los otros eventos se pueden insertar manualmente con record_activity()
desde donde tenga sentido (mañana se enchufa a doc upload, lesson, etc.).

Endpoint admin:
  POST /v1/activity/_record  · debugging only · admin/socio_senior
"""

from __future__ import annotations

import json
import logging
from typing import Optional
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/activity", tags=["activity"])


VALID_KINDS = {
    "comment_added", "comment_resolved", "comment_edited",
    "doc_uploaded", "doc_analyzed",
    "matter_status_changed", "matter_created", "matter_assigned",
    "lesson_extracted", "lesson_added",
    "kb_entry_added", "kb_entry_pinned",
    "event_added", "deadline_added", "deadline_completed",
    "invoice_sent", "invoice_paid",
    "signature_sent", "signature_signed",
    "other",
}


def _serialize(r) -> dict:
    return {
        "id": str(r["id"]),
        "ts": r["ts"].isoformat() if r["ts"] else None,
        "actor_user_id": str(r["actor_user_id"]) if r["actor_user_id"] else None,
        "actor_name": r["actor_name"],
        "actor_avatar": r["actor_avatar"],
        "kind": r["kind"],
        "matter_id": str(r["matter_id"]) if r["matter_id"] else None,
        "matter_document_id": str(r["matter_document_id"]) if r["matter_document_id"] else None,
        "target_kind": r["target_kind"],
        "target_id": str(r["target_id"]) if r["target_id"] else None,
        "title": r["title"],
        "preview": r["preview"],
        "payload": r["payload"] if not isinstance(r["payload"], str) else (json.loads(r["payload"]) if r["payload"] else {}),
    }


@router.get("")
async def feed(
    matter_id: Optional[str] = Query(default=None),
    actor_id: Optional[str] = Query(default=None),
    kinds: Optional[str] = Query(default=None, description="CSV de kinds"),
    since: Optional[str] = Query(default=None, description="ISO timestamp; sólo eventos posteriores"),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(get_current_firm),
):
    kinds_arr: Optional[list[str]] = None
    if kinds:
        kinds_arr = [k.strip() for k in kinds.split(",") if k.strip()]
        invalid = [k for k in kinds_arr if k not in VALID_KINDS]
        if invalid:
            raise HTTPException(400, f"kinds inválidos: {invalid}")
    since_dt: Optional[datetime] = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except Exception:
            raise HTTPException(400, "since debe ser ISO timestamp")

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select * from lexai_activity_feed($1::uuid, $2::uuid, $3::uuid, $4::text[], $5::timestamptz, $6)
            """,
            principal.firm_id, matter_id, actor_id, kinds_arr, since_dt, limit,
        )
    return {"items": [_serialize(r) for r in rows], "count": len(rows)}


@router.get("/matter/{matter_id}/counts")
async def matter_counts(
    matter_id: str,
    principal: Principal = Depends(get_current_firm),
):
    """Counts de colaboración para el matter (badges)."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {}
    async with storage.pool.acquire() as conn:
        result = await conn.fetchval(
            "select lexai_matter_collab_counts($1::uuid, $2::uuid)",
            principal.firm_id, matter_id,
        )
    if result is None:
        return {}
    if isinstance(result, str):
        try:
            return json.loads(result)
        except Exception:
            return {}
    return result


# ----------------------------------------------------------------
# Internal helper · usado por otros routers para registrar eventos manualmente
# ----------------------------------------------------------------
async def record_activity(
    firm_id: str,
    actor_user_id: Optional[str],
    kind: str,
    *,
    matter_id: Optional[str] = None,
    matter_document_id: Optional[str] = None,
    target_kind: Optional[str] = None,
    target_id: Optional[str] = None,
    title: Optional[str] = None,
    preview: Optional[str] = None,
    payload: Optional[dict] = None,
) -> None:
    """Inserta un activity_event · usado por otros routers (best-effort)."""
    if kind not in VALID_KINDS:
        logger.debug("record_activity · kind inválido %s, skip", kind)
        return
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return
    try:
        async with storage.pool.acquire() as conn:
            await conn.execute(
                """
                insert into activity_events
                  (firm_id, actor_user_id, kind, matter_id, matter_document_id,
                   target_kind, target_id, title, preview, payload)
                values ($1::uuid, $2::uuid, $3, $4::uuid, $5::uuid,
                        $6, $7::uuid, $8, $9, $10::jsonb)
                """,
                firm_id, actor_user_id, kind, matter_id, matter_document_id,
                target_kind, target_id, title, preview,
                json.dumps(payload or {}),
            )
    except Exception as e:
        logger.warning("record_activity failed (kind=%s): %s", kind, e)


class RecordIn(BaseModel):
    kind: str
    matter_id: Optional[str] = None
    matter_document_id: Optional[str] = None
    title: Optional[str] = None
    preview: Optional[str] = None
    payload: dict = Field(default_factory=dict)


@router.post("/_record", status_code=201)
async def record_endpoint(
    body: RecordIn,
    principal: Principal = Depends(get_current_firm),
):
    """Debugging · solo admin/socio. Registra manualmente un evento."""
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin / socio_senior")
    await record_activity(
        firm_id=principal.firm_id,
        actor_user_id=principal.user_id,
        kind=body.kind,
        matter_id=body.matter_id,
        matter_document_id=body.matter_document_id,
        title=body.title,
        preview=body.preview,
        payload=body.payload,
    )
    return {"ok": True}
