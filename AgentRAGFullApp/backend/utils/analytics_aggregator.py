"""Sprint 18 · Analytics aggregator.

Funciones puras que computan KPIs ejecutivos para un firm. Usadas por:
  - api/analytics_v2.py · GETs on-demand
  - agent/workers/snapshot_builder.py · job que congela snapshot diario

Cada función recibe una connection asyncpg ya adquirida (deja al caller
manejar transacciones / pool).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


async def compute_snapshot_payload(conn, firm_id: str) -> dict:
    """Recopila las métricas para un snapshot diario del firm.

    Devuelve un dict con las columnas que `firm_analytics_snapshots` espera
    (ints/numeric) + `payload` con breakdowns útiles (top abogados, etc.)
    """
    matters = await conn.fetchrow(
        """
        select
          count(*) as total,
          count(*) filter (where status = 'activo') as active,
          count(*) filter (where status in ('cerrado','archivado')) as closed
        from matters where firm_id = $1::uuid
        """,
        firm_id,
    )

    won_lost = await conn.fetchrow(
        """
        select
          count(*) filter (where outcome = 'won') as won,
          count(*) filter (where outcome = 'lost') as lost
        from case_lessons where firm_id = $1::uuid
        """,
        firm_id,
    )

    time_stats = await conn.fetchrow(
        """
        select
          coalesce(sum(duration_min) filter (where billable = true and ended_at is not null), 0) as billable_total,
          coalesce(sum(duration_min) filter (
            where billable = true and ended_at is not null
              and ended_at >= now() - interval '30 days'
          ), 0) as billable_30d,
          coalesce(sum(duration_min) filter (
            where billable = false and ended_at is not null
              and ended_at >= now() - interval '30 days'
          ), 0) as non_billable_30d
        from time_entries where firm_id = $1::uuid
        """,
        firm_id,
    )

    invoices_mtd = await conn.fetchrow(
        """
        select
          coalesce(sum(total_cop) filter (
            where status not in ('draft','void')
              and date_trunc('month', created_at) = date_trunc('month', now())
          ), 0) as invoiced_mtd,
          coalesce(sum(paid_amount_cop) filter (
            where paid_at is not null
              and date_trunc('month', paid_at) = date_trunc('month', now())
          ), 0) as collected_mtd
        from invoices where firm_id = $1::uuid
        """,
        firm_id,
    )

    ar = await conn.fetchrow(
        """
        select
          coalesce(sum(total_cop - paid_amount_cop) filter (
            where status in ('sent','partially_paid','overdue')
          ), 0) as ar_total,
          coalesce(sum(total_cop - paid_amount_cop) filter (
            where status in ('sent','partially_paid','overdue')
              and due_date is not null and due_date < current_date
          ), 0) as ar_overdue
        from invoices where firm_id = $1::uuid
        """,
        firm_id,
    )

    leads_row = await _safe_row(conn, """
        select
          count(*) filter (where status = 'open') as open,
          count(*) filter (where status = 'won' and updated_at >= now() - interval '30 days') as won_30d,
          count(*) filter (where status = 'lost' and updated_at >= now() - interval '30 days') as lost_30d
        from leads where firm_id = $1::uuid
    """, firm_id)

    preds_row = await _safe_row(conn, """
        select
          count(*) filter (where generated_at >= now() - interval '30 days') as p_30d,
          count(*) filter (where reviewed_at is not null and reviewed_at >= now() - interval '30 days') as reviewed_30d
        from case_predictions where firm_id = $1::uuid
    """, firm_id)

    tasks_row = await _safe_row(conn, """
        select
          count(*) filter (where status in ('open','in_progress','blocked')) as open,
          count(*) filter (where status in ('open','in_progress','blocked')
                          and due_at is not null and due_at < now()) as overdue
        from tasks where firm_id = $1::uuid
    """, firm_id)

    comments_row = await _safe_row(conn, """
        select count(*) filter (where created_at >= now() - interval '30 days') as c_30d
        from comments where firm_id = $1::uuid
    """, firm_id)

    kb_row = await _safe_row(conn, """
        select
          (select count(*) from knowledge_entries where firm_id = $1::uuid) as kb_total,
          (select count(*) from case_lessons where firm_id = $1::uuid) as lessons_total
    """, firm_id)

    # Breakdown: top 5 abogados por billable_minutes 30d
    top_lawyers = await _safe_fetch(conn, """
        select u.id, u.full_name, coalesce(sum(te.duration_min), 0) as bm
        from users u
        left join time_entries te on te.user_id = u.id and te.firm_id = $1::uuid
          and te.billable = true and te.ended_at is not null
          and te.ended_at >= now() - interval '30 days'
        where u.firm_id = $1::uuid
        group by u.id, u.full_name
        order by bm desc
        limit 5
    """, firm_id)

    return {
        "matters_total": int(matters["total"] or 0),
        "matters_active": int(matters["active"] or 0),
        "matters_closed": int(matters["closed"] or 0),
        "matters_won": int((won_lost or {}).get("won", 0) or 0) if won_lost else 0,
        "matters_lost": int((won_lost or {}).get("lost", 0) or 0) if won_lost else 0,
        "billable_minutes_total": int(time_stats["billable_total"] or 0),
        "billable_minutes_30d": int(time_stats["billable_30d"] or 0),
        "non_billable_minutes_30d": int(time_stats["non_billable_30d"] or 0),
        "invoiced_cop_mtd": float(invoices_mtd["invoiced_mtd"] or 0),
        "collected_cop_mtd": float(invoices_mtd["collected_mtd"] or 0),
        "ar_total_cop": float(ar["ar_total"] or 0),
        "ar_overdue_cop": float(ar["ar_overdue"] or 0),
        "leads_open": int((leads_row or {}).get("open", 0) or 0),
        "leads_won_30d": int((leads_row or {}).get("won_30d", 0) or 0),
        "leads_lost_30d": int((leads_row or {}).get("lost_30d", 0) or 0),
        "predictions_30d": int((preds_row or {}).get("p_30d", 0) or 0),
        "predictions_reviewed_30d": int((preds_row or {}).get("reviewed_30d", 0) or 0),
        "tasks_open": int((tasks_row or {}).get("open", 0) or 0),
        "tasks_overdue": int((tasks_row or {}).get("overdue", 0) or 0),
        "comments_30d": int((comments_row or {}).get("c_30d", 0) or 0),
        "kb_entries_total": int((kb_row or {}).get("kb_total", 0) or 0),
        "lessons_total": int((kb_row or {}).get("lessons_total", 0) or 0),
        "payload": {
            "top_lawyers": [
                {"user_id": str(r["id"]), "full_name": r["full_name"], "billable_minutes": int(r["bm"] or 0)}
                for r in top_lawyers
            ],
        },
    }


async def upsert_snapshot(conn, firm_id: str, snapshot_date: date) -> dict:
    """UPSERT del snapshot del día. Idempotente."""
    import json as _json
    payload = await compute_snapshot_payload(conn, firm_id)
    await conn.execute(
        """
        insert into firm_analytics_snapshots
          (firm_id, snapshot_date, matters_total, matters_active, matters_closed,
           matters_won, matters_lost,
           billable_minutes_total, billable_minutes_30d, non_billable_minutes_30d,
           invoiced_cop_mtd, collected_cop_mtd, ar_total_cop, ar_overdue_cop,
           leads_open, leads_won_30d, leads_lost_30d,
           predictions_30d, predictions_reviewed_30d,
           tasks_open, tasks_overdue, comments_30d,
           kb_entries_total, lessons_total, payload)
        values ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25::jsonb)
        on conflict (firm_id, snapshot_date) do update set
          matters_total = excluded.matters_total,
          matters_active = excluded.matters_active,
          matters_closed = excluded.matters_closed,
          matters_won = excluded.matters_won,
          matters_lost = excluded.matters_lost,
          billable_minutes_total = excluded.billable_minutes_total,
          billable_minutes_30d = excluded.billable_minutes_30d,
          non_billable_minutes_30d = excluded.non_billable_minutes_30d,
          invoiced_cop_mtd = excluded.invoiced_cop_mtd,
          collected_cop_mtd = excluded.collected_cop_mtd,
          ar_total_cop = excluded.ar_total_cop,
          ar_overdue_cop = excluded.ar_overdue_cop,
          leads_open = excluded.leads_open,
          leads_won_30d = excluded.leads_won_30d,
          leads_lost_30d = excluded.leads_lost_30d,
          predictions_30d = excluded.predictions_30d,
          predictions_reviewed_30d = excluded.predictions_reviewed_30d,
          tasks_open = excluded.tasks_open,
          tasks_overdue = excluded.tasks_overdue,
          comments_30d = excluded.comments_30d,
          kb_entries_total = excluded.kb_entries_total,
          lessons_total = excluded.lessons_total,
          payload = excluded.payload,
          computed_at = now()
        """,
        firm_id, snapshot_date,
        payload["matters_total"], payload["matters_active"], payload["matters_closed"],
        payload["matters_won"], payload["matters_lost"],
        payload["billable_minutes_total"], payload["billable_minutes_30d"], payload["non_billable_minutes_30d"],
        payload["invoiced_cop_mtd"], payload["collected_cop_mtd"],
        payload["ar_total_cop"], payload["ar_overdue_cop"],
        payload["leads_open"], payload["leads_won_30d"], payload["leads_lost_30d"],
        payload["predictions_30d"], payload["predictions_reviewed_30d"],
        payload["tasks_open"], payload["tasks_overdue"], payload["comments_30d"],
        payload["kb_entries_total"], payload["lessons_total"],
        _json.dumps(payload["payload"]),
    )
    return payload


async def _safe_row(conn, q, *args):
    try:
        return await conn.fetchrow(q, *args)
    except Exception as e:
        logger.debug("safe_row failed: %s", e)
        return None


async def _safe_fetch(conn, q, *args):
    try:
        return await conn.fetch(q, *args)
    except Exception as e:
        logger.debug("safe_fetch failed: %s", e)
        return []
