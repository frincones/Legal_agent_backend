"""Sprint 9 · Insight generator worker.

Genera `ai_insights` proactivos analizando el estado de la firma:
  - Reglas heurísticas (rápidas, deterministas)
  - Opcionalmente capa LLM para insights narrativos (si OPENAI_API_KEY)

Idempotente: usa (firm_id, kind, target_type, target_id) como dedup key — si ya
existe un insight `new` con la misma firma, no se duplica.

Tipos de insight generados:
  - deadline_unprep      · plazo en < 5 días sin time_entries asociados
  - matter_at_risk       · matter sin actividad > 60 días
  - high_value_client_inactive · cliente con > 3 matters ganados sin contacto > 90d
  - billing_opportunity  · matter con > 5h sin facturar > 30 días
  - lead_followup_overdue· lead con next_followup_at vencido
  - outdated_citation    · citation status='superada' aún en matter_document activo
"""

from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def generate_for_firm(firm_id: str) -> dict:
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage unavailable"}

    total_inserted = 0
    counts: dict[str, int] = {}

    async with storage.pool.acquire() as conn:
        # 1. Plazos próximos sin tiempo registrado
        rows = await conn.fetch(
            """
            select md.id as deadline_id, md.titulo, md.fecha, md.matter_id, m.titulo as matter_titulo
              from matter_deadlines md
              join matters m on m.id = md.matter_id
             where md.firm_id = $1::uuid
               and md.completado = false
               and md.fecha between current_date and current_date + 5
               and not exists (
                 select 1 from time_entries t
                  where t.matter_id = md.matter_id
                    and t.started_at >= now() - interval '7 days'
               )
             limit 30
            """,
            firm_id,
        )
        for r in rows:
            n = await _upsert(
                conn, firm_id,
                kind="deadline_unprep",
                severity="warning",
                target_type="matter",
                target_id=str(r["matter_id"]),
                title=f"Plazo cerca sin preparación: {r['titulo']}",
                body=f"El plazo '{r['titulo']}' del caso '{r['matter_titulo']}' vence el {r['fecha']} y no hay tiempo registrado en los últimos 7 días.",
                suggested_action="Abrir el caso y dedicar tiempo a preparar la actuación.",
                action_payload={"matter_id": str(r["matter_id"]), "navigate": f"/casos/{r['matter_id']}"},
                confidence=0.85,
            )
            total_inserted += n
        counts["deadline_unprep"] = len(rows)

        # 2. Matters at risk (sin actividad > 60 días)
        rows = await conn.fetch(
            """
            select m.id, m.titulo, m.client_id, m.updated_at
              from matters m
             where m.firm_id = $1::uuid
               and m.status = 'activo'
               and m.updated_at < now() - interval '60 days'
               and not exists (
                 select 1 from time_entries t
                  where t.matter_id = m.id
                    and t.started_at >= now() - interval '60 days'
               )
             limit 20
            """,
            firm_id,
        )
        for r in rows:
            n = await _upsert(
                conn, firm_id,
                kind="matter_at_risk",
                severity="warning",
                target_type="matter",
                target_id=str(r["id"]),
                title=f"Caso sin movimiento: {r['titulo']}",
                body=f"El caso '{r['titulo']}' no tiene actividad desde hace más de 60 días. Verifica con el cliente si sigue activo.",
                suggested_action="Contactar al cliente o archivar el caso.",
                action_payload={"matter_id": str(r["id"]), "navigate": f"/casos/{r['id']}"},
                confidence=0.70,
            )
            total_inserted += n
        counts["matter_at_risk"] = len(rows)

        # 3. Billing opportunity
        rows = await conn.fetch(
            """
            select m.id, m.titulo,
                   sum(t.duration_min) as total_min,
                   max(t.started_at) as last_entry
              from matters m
              join time_entries t on t.matter_id = m.id and t.invoice_line_id is null and t.billable = true
             where m.firm_id = $1::uuid
             group by m.id, m.titulo
            having sum(t.duration_min) >= 300                    -- 5 horas+
               and max(t.started_at) < now() - interval '30 days'
             limit 20
            """,
            firm_id,
        )
        for r in rows:
            hours = (r["total_min"] or 0) / 60
            n = await _upsert(
                conn, firm_id,
                kind="billing_opportunity",
                severity="info",
                target_type="matter",
                target_id=str(r["id"]),
                title=f"Horas sin facturar: {hours:.1f}h en {r['titulo']}",
                body=f"Tienes {hours:.1f} horas facturables en '{r['titulo']}' que no se han facturado.",
                suggested_action="Generar factura del periodo.",
                action_payload={"matter_id": str(r["id"]), "navigate": "/facturacion"},
                confidence=0.95,
            )
            total_inserted += n
        counts["billing_opportunity"] = len(rows)

        # 4. Leads con followup vencido
        rows = await conn.fetch(
            """
            select id, nombre, next_followup_at
              from leads
             where firm_id = $1::uuid
               and status = 'open'
               and next_followup_at is not null
               and next_followup_at < now()
             limit 30
            """,
            firm_id,
        )
        for r in rows:
            n = await _upsert(
                conn, firm_id,
                kind="lead_followup_overdue",
                severity="warning",
                target_type="lead",
                target_id=str(r["id"]),
                title=f"Lead sin seguimiento: {r['nombre']}",
                body=f"El prospecto {r['nombre']} tiene un follow-up programado para {r['next_followup_at']} que ya venció.",
                suggested_action="Contactar al prospecto o reprogramar follow-up.",
                action_payload={"lead_id": str(r["id"]), "navigate": "/leads"},
                confidence=0.95,
            )
            total_inserted += n
        counts["lead_followup_overdue"] = len(rows)

        # 5. Citas superadas en matter_documents activos
        try:
            rows = await conn.fetch(
                """
                select dc.id, dc.rubro_inserted as ref, md.matter_id, md.titulo
                  from document_citations dc
                  join matter_documents md on md.id = dc.matter_document_id
                 where md.firm_id = $1::uuid
                   and dc.status = 'superada'
                 limit 20
                """,
                firm_id,
            )
            for r in rows:
                n = await _upsert(
                    conn, firm_id,
                    kind="outdated_citation",
                    severity="warning",
                    target_type="document",
                    target_id=str(r["matter_id"]),
                    title=f"Cita superada en {r['titulo']}",
                    body=f"El documento '{r['titulo']}' cita {r['ref']}, que ya fue superada por jurisprudencia posterior.",
                    suggested_action="Reemplazar la cita por la sentencia vigente.",
                    action_payload={"matter_id": str(r["matter_id"]), "navigate": f"/casos/{r['matter_id']}/canvas"},
                    confidence=0.80,
                )
                total_inserted += n
            counts["outdated_citation"] = len(rows)
        except Exception as e:
            logger.debug("outdated_citation skipped: %s", e)

    return {"ok": True, "inserted": total_inserted, "checks": counts}


async def _upsert(conn, firm_id, kind, severity, target_type, target_id,
                  title, body, suggested_action, action_payload, confidence) -> int:
    """Inserta si no hay otro `new` igual para mismo target."""
    existing = await conn.fetchval(
        """
        select 1 from ai_insights
         where firm_id = $1::uuid and kind = $2 and target_type = $3
           and target_id = $4::uuid and status = 'new'
         limit 1
        """,
        firm_id, kind, target_type, target_id,
    )
    if existing:
        return 0
    await conn.execute(
        """
        insert into ai_insights
          (firm_id, kind, severity, target_type, target_id, title, body,
           suggested_action, action_payload, confidence, generated_by)
        values ($1::uuid, $2, $3, $4, $5::uuid, $6, $7, $8, $9::jsonb, $10, 'rules')
        """,
        firm_id, kind, severity, target_type, target_id,
        title, body, suggested_action,
        json.dumps(action_payload or {}), confidence,
    )
    return 1


async def generate_for_all_firms() -> dict:
    """Cron entrypoint nightly."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage unavailable"}
    async with storage.pool.acquire() as conn:
        firms = await conn.fetch("select id from firms")
    total = 0
    for r in firms:
        try:
            res = await generate_for_firm(str(r["id"]))
            total += res.get("inserted", 0) or 0
        except Exception as e:
            logger.warning("insight gen failed for %s: %s", r["id"], e)
    return {"firms": len(firms), "insights_inserted": total}
