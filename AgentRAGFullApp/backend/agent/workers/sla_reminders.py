"""Sprint 5 · SLA reminders worker.

Lee `matter_deadlines` no completados con fecha < 48h y dispara push notifications
a los usuarios responsables del matter (matter_responsibles). Idempotente: no
re-envía un push si ya hay un legal_alert dismissed con target_ref idéntico
en las últimas 24h.

Punto de entrada:
  POST /v1/sla/run-now      (admin only)
  Voice tool: 'LexAI, revisa SLAs'
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


async def run_sla_reminders(firm_id: str) -> dict:
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}

    now = datetime.now(timezone.utc)
    in_48h = now + timedelta(hours=48)

    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select md.id, md.titulo, md.fecha, md.matter_id,
                   m.titulo as matter_titulo, m.owner_user_id
              from matter_deadlines md
              join matters m on m.id = md.matter_id
             where md.firm_id = $1::uuid
               and md.completado = false
               and md.fecha is not null
               and md.fecha::timestamptz <= $2::timestamptz
               and md.fecha::timestamptz >= now() - interval '1 day'
             order by md.fecha asc
            """,
            firm_id, in_48h,
        )

    if not rows:
        return {"checked": 0, "pushed": 0, "skipped_recent": 0}

    pushed = 0
    skipped = 0
    from api.push import dispatch_to_user
    for r in rows:
        owner_id = r["owner_user_id"]
        users = [{"user_id": owner_id}] if owner_id else []
        async with storage.pool.acquire() as conn:
            already = await conn.fetchrow(
                """
                select id from legal_alerts
                 where firm_id = $1::uuid
                   and target_ref = $2
                   and source = 'rules'
                   and detected_at > now() - interval '24 hours'
                 limit 1
                """,
                firm_id, f"deadline:{r['id']}",
            )
        if already:
            skipped += 1
            continue
        title = "Plazo procesal vence pronto"
        body = f"{r['titulo']} ({r['matter_titulo']}) vence {r['fecha'].isoformat()}"
        for u in users:
            try:
                result = await dispatch_to_user(
                    user_id=str(u["user_id"]),
                    firm_id=firm_id,
                    title=title,
                    body=body,
                    url=f"/casos/{r['matter_id']}",
                )
                if result.get("sent", 0) > 0:
                    pushed += 1
            except Exception as e:
                logger.warning("sla push failed: %s", e)
        # Marcar como notificado vía legal_alerts (auditable)
        try:
            async with storage.pool.acquire() as conn:
                await conn.execute(
                    """
                    insert into legal_alerts
                      (firm_id, target_type, target_ref, kind, severity,
                       title, description, source, affected_matter_ids)
                    values ($1::uuid, 'tema', $2, 'cambio_normativo', 'warning',
                            $3, $4, 'rules', array[$5::uuid])
                    on conflict do nothing
                    """,
                    firm_id, f"deadline:{r['id']}", title, body, r["matter_id"],
                )
        except Exception as e:
            logger.debug("legal_alerts insert skipped: %s", e)

    return {"checked": len(rows), "pushed": pushed, "skipped_recent": skipped}


async def run_sla_reminders_tool(args: dict, ctx: dict) -> dict:
    firm_id = ctx.get("firm_id") or args.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    return await run_sla_reminders(firm_id)
