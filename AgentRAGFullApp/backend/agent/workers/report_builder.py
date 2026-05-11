"""Sprint 7 · Report builder worker.

Pre-computa agregados pesados y los cachea en `firm_reports`. Ideal para
correr nightly via Railway cron o bajo demanda desde el endpoint admin.

Reportes producidos:
  · overview     · KPIs generales del periodo
  · lawyer_perf  · performance individual de cada abogado
  · deadlines    · timeline cumplido/vencido
  · citations    · verified/outdated/pending por mes
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


async def refresh_firm_reports(firm_id: str, days: int = 30) -> dict:
    """Refresca los 4 reportes del firm. Idempotente: hace upsert."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage unavailable"}

    period_end = date.today()
    period_start = period_end - timedelta(days=days)

    async with storage.pool.acquire() as conn:
        # overview
        overview = await conn.fetchval(
            "select lexai_firm_kpis($1::uuid, $2::int)", firm_id, days
        )
        await _upsert_report(conn, firm_id, "overview", period_start, period_end, overview or {})

        # lawyer_perf
        lawyer_rows = await conn.fetch(
            """
            with d as (select (now() - ($2::text || ' days')::interval) as ts)
            select u.id, u.full_name, u.role,
              (select count(*) from matters m
                 where m.firm_id = $1::uuid and m.owner_user_id = u.id and m.status = 'activo') as active,
              (select count(*) from matters m
                 where m.firm_id = $1::uuid and m.owner_user_id = u.id
                   and m.created_at >= (select ts from d)) as new,
              (select count(*) from matter_deadlines md join matters m2 on m2.id = md.matter_id
                 where m2.owner_user_id = u.id and md.completado = true
                   and md.fecha >= (select ts from d)::date) as done,
              (select count(*) from matter_deadlines md join matters m2 on m2.id = md.matter_id
                 where m2.owner_user_id = u.id and md.completado = false
                   and md.fecha < current_date) as overdue
              from users u
             where u.firm_id = $1::uuid
             order by active desc
            """,
            firm_id, days,
        )
        lawyer_data = {
            "items": [
                {
                    "user_id": str(r["id"]), "full_name": r["full_name"], "role": r["role"],
                    "active": r["active"], "new": r["new"],
                    "done": r["done"], "overdue": r["overdue"],
                }
                for r in lawyer_rows
            ],
        }
        await _upsert_report(conn, firm_id, "lawyer_perf", period_start, period_end, lawyer_data)

        # deadlines timeline
        dl_rows = await conn.fetch(
            """
            select date_trunc('day', fecha)::date as day,
                   count(*) filter (where completado = true) as done,
                   count(*) filter (where completado = false and fecha < current_date) as overdue
              from matter_deadlines
             where firm_id = $1::uuid and fecha >= current_date - $2::int
             group by day order by day
            """,
            firm_id, days,
        )
        dl_data = {
            "items": [
                {"day": r["day"].isoformat() if r["day"] else None,
                 "done": r["done"], "overdue": r["overdue"]}
                for r in dl_rows
            ]
        }
        await _upsert_report(conn, firm_id, "deadlines", period_start, period_end, dl_data)

        # citations
        cit_rows = await conn.fetch(
            """
            select status, count(*) as n
              from document_citations dc
              join matter_documents md on md.id = dc.matter_document_id
             where md.firm_id = $1::uuid
               and dc.created_at >= now() - ($2::text || ' days')::interval
             group by status
            """,
            firm_id, days,
        )
        cit_data = {"items": [{"status": r["status"], "count": r["n"]} for r in cit_rows]}
        await _upsert_report(conn, firm_id, "citations", period_start, period_end, cit_data)

    return {"ok": True, "period_start": period_start.isoformat(), "period_end": period_end.isoformat()}


async def _upsert_report(conn, firm_id, kind, p_start, p_end, data):
    await conn.execute(
        """
        insert into firm_reports (firm_id, kind, period_start, period_end, data, generated_at)
        values ($1::uuid, $2, $3::date, $4::date, $5::jsonb, now())
        on conflict (firm_id, kind, period_start, period_end) do update set
          data = excluded.data, generated_at = now()
        """,
        firm_id, kind, p_start, p_end, json.dumps(data),
    )


async def refresh_all_firms() -> dict:
    """Para correr desde cron Railway: refresca todas las firmas activas."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage unavailable"}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch("select id from firms")
    total = 0
    for r in rows:
        try:
            await refresh_firm_reports(str(r["id"]))
            total += 1
        except Exception as e:
            logger.warning("refresh firm %s failed: %s", r["id"], e)
    return {"firms_refreshed": total}
