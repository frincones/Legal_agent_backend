"""Sprint 5 · Judicial API · poll-now + lookup directo (sin suscripción).

Endpoints livianos pensados para Court Watcher dashboard y voice tools.

  POST /v1/judicial/poll-now         · fuerza poll de todas las suscripciones
  GET  /v1/judicial/lookup           · lookup one-shot por expediente (live source)
  GET  /v1/judicial/snapshots/{id}   · historial raw + diff de una suscripción
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/judicial", tags=["judicial"])


@router.post("/poll-now")
async def poll_now(principal: Principal = Depends(get_current_firm)):
    from agent.workers.judicial_poller import poll_firm
    return await poll_firm(principal.firm_id)


@router.get("/lookup")
async def lookup_expediente(
    expediente: str = Query(min_length=2, max_length=80),
    fuente: str = Query(default="rama_judicial_live", regex="^(rama_judicial_live|rama_judicial_demo|dof_co_demo)$"),
    principal: Principal = Depends(get_current_firm),
):
    """Lookup one-shot — no requiere suscripción. Útil antes de suscribirse."""
    if fuente == "rama_judicial_live":
        from legal_sources.judicial.rama_judicial import RamaJudicialLiveSource
        source = RamaJudicialLiveSource()
    elif fuente == "rama_judicial_demo":
        from legal_sources.judicial.rama_judicial import RamaJudicialDemoSource
        source = RamaJudicialDemoSource()
    else:
        from legal_sources.judicial.dof_co import DofCoDemoSource
        source = DofCoDemoSource()
    try:
        notifs = await source.poll(expediente=expediente, last_polled_at=None)
    except Exception as e:
        logger.warning("lookup failed: %s", e)
        raise HTTPException(502, f"source error: {e}")
    return {
        "expediente": expediente,
        "fuente": fuente,
        "count": len(notifs),
        "items": [
            {
                "titulo": n.titulo,
                "fecha_publicacion": n.fecha_publicacion.isoformat() if n.fecha_publicacion else None,
                "fecha_actuacion": n.fecha_actuacion.isoformat() if n.fecha_actuacion else None,
                "expediente": n.expediente,
                "juzgado": n.juzgado,
                "tipo": n.tipo,
                "severidad": n.severidad,
                "resumen": n.resumen,
                "url_oficial": n.url_oficial,
            }
            for n in notifs
        ],
    }


@router.get("/snapshots/{subscription_id}")
async def list_snapshots(
    subscription_id: str,
    limit: int = Query(default=20, le=100),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, fetched_at, status_code, raw_hash, diff_changed,
                   notif_count, parsed
              from judicial_snapshots
             where firm_id = $1::uuid and subscription_id = $2::uuid
             order by fetched_at desc
             limit $3
            """,
            principal.firm_id, subscription_id, limit,
        )
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]),
                "fetched_at": r["fetched_at"].isoformat() if r["fetched_at"] else None,
                "status_code": r["status_code"],
                "raw_hash": r["raw_hash"],
                "diff_changed": r["diff_changed"],
                "notif_count": r["notif_count"],
                "parsed": r["parsed"],
            }
            for r in rows
        ],
    }
