"""Judicial poller · orquesta las fuentes y persiste en judicial_notifications.

Diseño:
  - poll_subscription(): polea UNA suscripción
  - poll_all_active(): itera todas las activas (pensado para invocación
    on-demand desde un endpoint admin o un cron en Railway)
  - dedup por hash_dedup (sha256(fuente|expediente|fecha|titulo))
  - actualiza judicial_subscriptions.last_polled_at + last_status

No usa schedulers internos (apscheduler) para no acoplarse a infra. La
orquestación temporal puede venir de:
  - Railway cron (preferido)
  - Llamada manual desde admin endpoint
  - Voice tool ("LexAI, busca novedades en mis expedientes")
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)


_SOURCES_CACHE: dict[str, object] = {}


def _get_source(name: str):
    """Lazy registry of judicial sources."""
    if name in _SOURCES_CACHE:
        return _SOURCES_CACHE[name]
    if name == "rama_judicial_demo":
        from legal_sources.judicial.rama_judicial import RamaJudicialDemoSource
        s = RamaJudicialDemoSource()
    elif name == "rama_judicial_live":
        from legal_sources.judicial.rama_judicial import RamaJudicialLiveSource
        s = RamaJudicialLiveSource()
    elif name == "dof_co_demo":
        from legal_sources.judicial.dof_co import DofCoDemoSource
        s = DofCoDemoSource()
    else:
        raise ValueError(f"unknown judicial source: {name}")
    _SOURCES_CACHE[name] = s
    return s


async def poll_subscription(subscription_id: str) -> dict:
    """Polea una suscripción y persiste sus notificaciones nuevas."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}

    async with storage.pool.acquire() as conn:
        sub = await conn.fetchrow(
            """
            select id, firm_id, matter_id, fuente, expediente, juzgado,
                   ciudad, query_extra, last_polled_at, active
            from judicial_subscriptions
            where id = $1::uuid
            """,
            subscription_id,
        )
    if not sub:
        return {"error": "subscription not found"}
    if not sub["active"]:
        return {"skipped": "inactive"}

    try:
        source = _get_source(sub["fuente"])
    except ValueError as e:
        return {"error": str(e)}

    last_date: Optional[date] = (
        sub["last_polled_at"].date() if isinstance(sub["last_polled_at"], datetime) else None
    )
    try:
        notifs = await source.poll(
            expediente=sub["expediente"] or "",
            last_polled_at=last_date,
        )
    except Exception as e:
        logger.exception("poll source failed: %s", e)
        await _mark_polled(subscription_id, status="error", error=str(e))
        return {"error": str(e), "n_new": 0}

    inserted = 0
    async with storage.pool.acquire() as conn:
        for n in notifs:
            try:
                await conn.execute(
                    """
                    insert into judicial_notifications
                      (firm_id, matter_id, subscription_id, fuente, titulo,
                       resumen, url_oficial, fecha_publicacion, fecha_actuacion,
                       expediente, juzgado, tipo, severidad, hash_dedup, metadata)
                    values
                      ($1::uuid, $2::uuid, $3::uuid, $4, $5,
                       $6, $7, $8::date, $9::date,
                       $10, $11, $12, $13, $14, $15::jsonb)
                    on conflict (firm_id, hash_dedup) do nothing
                    """,
                    sub["firm_id"], sub["matter_id"], subscription_id,
                    n.fuente, n.titulo, n.resumen, n.url_oficial,
                    n.fecha_publicacion, n.fecha_actuacion,
                    n.expediente, n.juzgado, n.tipo, n.severidad,
                    n.hash_dedup(), json.dumps(n.metadata),
                )
                inserted += 1
            except Exception as e:
                logger.debug("insert notification skipped: %s", e)

    # Snapshot · raw_hash sobre todas las notifs para detectar diffs novel.
    try:
        normalized = "|".join(sorted(n.hash_dedup() for n in notifs))
        raw_hash = hashlib.sha256(normalized.encode()).hexdigest()
        raw_body = json.dumps(
            [
                {
                    "titulo": n.titulo,
                    "fecha": n.fecha_publicacion.isoformat(),
                    "tipo": n.tipo,
                }
                for n in notifs
            ],
            ensure_ascii=False,
        )[:65000]
        async with storage.pool.acquire() as conn:
            prev = await conn.fetchrow(
                """
                select raw_hash from judicial_snapshots
                 where subscription_id = $1::uuid
                 order by fetched_at desc
                 limit 1
                """,
                subscription_id,
            )
            changed = (not prev) or (prev["raw_hash"] != raw_hash)
            await conn.execute(
                """
                insert into judicial_snapshots
                  (firm_id, subscription_id, status_code, raw_hash,
                   raw_body, parsed, diff_changed, notif_count)
                values ($1::uuid, $2::uuid, $3, $4, $5, $6::jsonb, $7, $8)
                """,
                sub["firm_id"], subscription_id, 200, raw_hash, raw_body,
                json.dumps({"count": len(notifs)}),
                changed, len(notifs),
            )
    except Exception as e:
        logger.debug("snapshot write skipped: %s", e)

    await _mark_polled(subscription_id, status="ok", error=None)
    return {"n_polled": len(notifs), "n_inserted": inserted}


async def _mark_polled(sub_id: str, status: str, error: Optional[str]) -> None:
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            update judicial_subscriptions
               set last_polled_at = now(),
                   last_status = $2,
                   last_error = $3,
                   poll_count = poll_count + 1
             where id = $1::uuid
            """,
            sub_id, status, error,
        )


async def poll_firm(firm_id: str) -> dict:
    """Polea todas las suscripciones activas de un firm. Devuelve resumen."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            "select id from judicial_subscriptions where firm_id = $1::uuid and active = true",
            firm_id,
        )
    total_inserted = 0
    polled = 0
    errors: list[str] = []
    for r in rows:
        result = await poll_subscription(str(r["id"]))
        polled += 1
        total_inserted += result.get("n_inserted", 0) or 0
        if "error" in result:
            errors.append(result["error"])
    return {"polled": polled, "inserted": total_inserted, "errors": errors}
