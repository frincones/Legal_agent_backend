"""Paralegal-grade tools for the LexAI voice agent.

These extend the core 7 tools (research_jurisprudence, validate_citation,
validate_norm_vigencia, calc_liquidacion, draft_pleading, request_human_approval,
open_matter_context) with the day-to-day operations a Colombian lawyer
delegates to a paralegal:

    list_my_matters      ─ "¿qué casos tengo activos?"
    find_client          ─ "busca el cliente Rodríguez"
    list_upcoming_deadlines ─ "¿qué vence esta semana?"
    add_matter_note      ─ "agrega esta nota al caso"
    add_matter_deadline  ─ "agenda audiencia el lunes 9 a las 10am"
    mark_deadline_done   ─ "marca como completada la audiencia de ayer"
    list_matter_documents─ "lista los documentos del caso"
    summarize_document   ─ "dame el resumen del documento X"
    list_pending_hitl    ─ "qué tengo pendiente de aprobar"
    get_firm_metrics     ─ "cómo va mi mes en LexAI"

All tools accept (args: dict, ctx: dict) where ctx contains firm_id,
user_id, matter_id, session_id. Every query is scoped by firm_id.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.tools._ui_events import ui_data_changed

logger = logging.getLogger(__name__)


async def _pool():
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise RuntimeError("storage not available")
    return storage.pool


def _err(msg: str) -> dict:
    return {"error": msg}


# ─────────────────────────────────────────────────────────────────────
# Matters
# ─────────────────────────────────────────────────────────────────────


async def list_my_matters_tool(args: dict, ctx: dict) -> dict:
    """List the user's active cases, optionally filtered by materia or priority."""
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return _err("firm_id required")
    materia = args.get("materia")
    priority = args.get("priority")
    limit = max(1, min(int(args.get("limit") or 10), 25))

    pool = await _pool()
    where = ["m.firm_id = $1::uuid", "m.status != 'archivado'"]
    params: list[Any] = [firm_id]
    if materia:
        params.append(materia)
        where.append(f"m.materia = ${len(params)}")
    if priority:
        params.append(priority)
        where.append(f"m.priority = ${len(params)}")
    params.append(limit)

    sql = f"""
        select m.id, m.display_id, m.titulo, m.materia, m.etapa_procesal,
               m.priority, m.proxima_fecha, m.proxima_tipo, m.tribunal,
               c.nombre as cliente_nombre
        from matters m
        join clients c on c.id = m.client_id
        where {' and '.join(where)}
        order by case m.priority when 'alta' then 1 when 'media' then 2 else 3 end,
                 m.proxima_fecha nulls last
        limit ${len(params)}
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {
        "count": len(rows),
        "matters": [
            {
                "id": str(r["id"]),
                "display_id": r["display_id"],
                "titulo": r["titulo"],
                "materia": str(r["materia"]),
                "etapa": r["etapa_procesal"],
                "priority": str(r["priority"]),
                "cliente": r["cliente_nombre"],
                "tribunal": r["tribunal"],
                "proxima_fecha": r["proxima_fecha"].isoformat() if r["proxima_fecha"] else None,
                "proxima_tipo": r["proxima_tipo"],
            }
            for r in rows
        ],
    }


# ─────────────────────────────────────────────────────────────────────
# Clients
# ─────────────────────────────────────────────────────────────────────


async def find_client_tool(args: dict, ctx: dict) -> dict:
    """Search clients by partial name, NIT or cédula. Returns top 6."""
    firm_id = ctx.get("firm_id")
    q = (args.get("query") or "").strip()
    if not firm_id or not q:
        return _err("firm_id and query required")

    pool = await _pool()
    pattern = f"%{q}%"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select c.id, c.nombre, c.tipo, c.tax_id, c.personal_id,
                   c.email, c.telefono, c.vip,
                   (select count(*) from matters m where m.client_id = c.id and m.status != 'archivado') as casos_activos
            from clients c
            where c.firm_id = $1::uuid
              and (
                public.lexai_unaccent_lower(c.nombre) ilike public.lexai_unaccent_lower($2)
                or coalesce(c.tax_id,'') ilike $2
                or coalesce(c.personal_id,'') ilike $2
              )
            order by c.vip desc, c.nombre
            limit 6
            """,
            firm_id, pattern,
        )
    return {
        "count": len(rows),
        "clients": [
            {
                "id": str(r["id"]),
                "nombre": r["nombre"],
                "tipo": str(r["tipo"]),
                "tax_id": r["tax_id"],
                "personal_id": r["personal_id"],
                "email": r["email"],
                "telefono": r["telefono"],
                "vip": r["vip"],
                "casos_activos": r["casos_activos"],
            }
            for r in rows
        ],
    }


# ─────────────────────────────────────────────────────────────────────
# Deadlines
# ─────────────────────────────────────────────────────────────────────


async def list_upcoming_deadlines_tool(args: dict, ctx: dict) -> dict:
    """Upcoming pending deadlines for the firm in the next N days."""
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return _err("firm_id required")
    days = max(1, min(int(args.get("days") or 7), 60))
    matter_id = args.get("matter_id")

    pool = await _pool()
    sql = """
        select d.id, d.matter_id, d.titulo, d.fecha, d.tipo, m.titulo as matter_titulo,
               m.display_id
        from matter_deadlines d
        join matters m on m.id = d.matter_id
        where d.firm_id = $1::uuid
          and d.completado = false
          and d.fecha >= now()
          and d.fecha <= now() + make_interval(days => $2)
    """
    params: list[Any] = [firm_id, int(days)]
    if matter_id:
        params.append(matter_id)
        sql += f" and d.matter_id = ${len(params)}::uuid"
    sql += " order by d.fecha asc limit 25"

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {
        "count": len(rows),
        "window_days": days,
        "deadlines": [
            {
                "id": str(r["id"]),
                "matter_id": str(r["matter_id"]),
                "matter": r["matter_titulo"],
                "matter_display_id": r["display_id"],
                "titulo": r["titulo"],
                "tipo": r["tipo"],
                "fecha": r["fecha"].isoformat(),
                "dias_restantes": (r["fecha"] - datetime.now(timezone.utc)).days,
            }
            for r in rows
        ],
    }


async def add_matter_deadline_tool(args: dict, ctx: dict) -> dict:
    """Schedule a new deadline / hearing on the matter calendar."""
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    titulo = (args.get("titulo") or "").strip()
    fecha = args.get("fecha")
    tipo = args.get("tipo") or "audiencia"
    if not (firm_id and user_id and matter_id and titulo and fecha):
        return _err("matter_id, titulo, fecha required")
    try:
        if isinstance(fecha, str):
            fecha_dt = datetime.fromisoformat(fecha.replace("Z", "+00:00"))
        else:
            fecha_dt = fecha
    except Exception as e:
        return _err(f"fecha inválida: {e}")

    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into matter_deadlines (matter_id, firm_id, titulo, fecha, tipo, origen, completado)
            values ($1::uuid, $2::uuid, $3, $4, $5, 'voice', false)
            returning id
            """,
            matter_id, firm_id, titulo, fecha_dt, tipo,
        )
        try:
            await conn.execute(
                """
                insert into matter_timeline (matter_id, firm_id, kind, ts, payload)
                values ($1::uuid, $2::uuid, 'deadline_added', now(), $3::jsonb)
                """,
                matter_id, firm_id,
                json.dumps({"deadline_id": str(row["id"]), "titulo": titulo, "fecha": fecha_dt.isoformat(), "tipo": tipo, "by": user_id}),
            )
        except Exception:
            # timeline insert is best-effort; missing column shouldn't fail the deadline.
            pass
    return {
        "id": str(row["id"]), "matter_id": matter_id, "titulo": titulo,
        "fecha": fecha_dt.isoformat(), "tipo": tipo,
        "_ui_command": ui_data_changed(
            "deadlines", matter_id=matter_id, firm_id=firm_id, op="create",
            extra={"deadline_id": str(row["id"])},
        ),
    }


async def mark_deadline_done_tool(args: dict, ctx: dict) -> dict:
    """Check off a deadline as completed."""
    firm_id = ctx.get("firm_id")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    deadline_id = args.get("deadline_id")
    if not firm_id:
        return _err("firm_id required")
    # Si el LLM pasó un string que no es UUID (ej "audiencia_conciliacion"),
    # ignóralo y aplica fallback de búsqueda.
    if deadline_id:
        import re as _re
        if not _re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            str(deadline_id).lower(),
        ):
            deadline_id = None  # no es UUID válido, forzar fallback
    if not deadline_id:
        # Infiere el deadline próximo no-completado del matter (o el más reciente).
        pool = await _pool()
        async with pool.acquire() as conn:
            if matter_id:
                row = await conn.fetchrow(
                    """select id from matter_deadlines
                        where firm_id=$1::uuid and matter_id=$2::uuid
                          and completado is not true
                        order by fecha asc limit 1""",
                    firm_id, matter_id,
                )
            else:
                row = await conn.fetchrow(
                    """select id from matter_deadlines
                        where firm_id=$1::uuid and completado is not true
                        order by fecha asc limit 1""",
                    firm_id,
                )
        if not row:
            return _err("no encontré deadlines abiertos")
        deadline_id = str(row["id"])

    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update matter_deadlines
               set completado = true,
                   metadata = coalesce(metadata,'{}'::jsonb) || jsonb_build_object('completed_at', now())
             where id = $1::uuid and firm_id = $2::uuid
            returning matter_id, titulo
            """,
            deadline_id, firm_id,
        )
    if not row:
        return _err("deadline not found")
    return {
        "id": deadline_id, "matter_id": str(row["matter_id"]), "titulo": row["titulo"],
        "completado": True,
        "_ui_command": ui_data_changed(
            "deadlines", matter_id=str(row["matter_id"]), firm_id=firm_id, op="update",
            extra={"deadline_id": deadline_id, "completed": True},
        ),
    }


# ─────────────────────────────────────────────────────────────────────
# Notes
# ─────────────────────────────────────────────────────────────────────


async def add_matter_note_tool(args: dict, ctx: dict) -> dict:
    """Append a free-text note to a matter (typically dictated by voice)."""
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    # Tolerante: body, text, content, note, prompt o user_prompt del ctx.
    body = (
        args.get("body") or args.get("text") or args.get("content")
        or args.get("note") or args.get("prompt")
        or ctx.get("user_prompt") or ""
    ).strip()
    if not (firm_id and user_id and matter_id and body):
        return _err("matter_id and body required")

    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into matter_notes (matter_id, firm_id, author_user_id, body)
            values ($1::uuid, $2::uuid, $3::uuid, $4)
            returning id, created_at
            """,
            matter_id, firm_id, user_id, body,
        )
    return {
        "id": str(row["id"]), "matter_id": matter_id,
        "created_at": row["created_at"].isoformat(),
        "_ui_command": ui_data_changed(
            "notes", matter_id=matter_id, firm_id=firm_id, op="create",
            extra={"note_id": str(row["id"])},
        ),
    }


# ─────────────────────────────────────────────────────────────────────
# Documents
# ─────────────────────────────────────────────────────────────────────


async def list_matter_documents_tool(args: dict, ctx: dict) -> dict:
    """List documents on a matter sorted by recency."""
    firm_id = ctx.get("firm_id")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    if not (firm_id and matter_id):
        return _err("matter_id required")

    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, kind, titulo, status, pages, byte_size, created_at,
                   substring(coalesce(resumen_ia,'') for 320) as resumen_preview
            from matter_documents
            where matter_id = $1::uuid and firm_id = $2::uuid
            order by created_at desc
            limit 20
            """,
            matter_id, firm_id,
        )
    return {
        "count": len(rows),
        "documents": [
            {
                "id": str(r["id"]),
                "kind": r["kind"],
                "titulo": r["titulo"],
                "status": r["status"],
                "pages": r["pages"],
                "byte_size": r["byte_size"],
                "created_at": r["created_at"].isoformat(),
                "resumen": r["resumen_preview"],
            }
            for r in rows
        ],
    }


async def summarize_document_tool(args: dict, ctx: dict) -> dict:
    """Return the cached IA summary of a document, or trigger one if missing."""
    firm_id = ctx.get("firm_id")
    doc_id = args.get("document_id")
    if not (firm_id and doc_id):
        return _err("document_id required")

    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select titulo, resumen_ia, status, pages
            from matter_documents
            where id = $1::uuid and firm_id = $2::uuid
            """,
            doc_id, firm_id,
        )
    if not row:
        return _err("document not found")
    if row["resumen_ia"]:
        return {
            "document_id": doc_id,
            "titulo": row["titulo"],
            "resumen": row["resumen_ia"],
            "pages": row["pages"],
            "status": row["status"],
            "cached": True,
        }
    # Cold path: tell the agent the summary is being generated.
    return {
        "document_id": doc_id,
        "titulo": row["titulo"],
        "status": row["status"],
        "pages": row["pages"],
        "resumen": None,
        "cached": False,
        "hint": "El resumen IA aún no se generó. El OCR worker procesa el documento; vuelve a preguntar en unos segundos.",
    }


# ─────────────────────────────────────────────────────────────────────
# HITL
# ─────────────────────────────────────────────────────────────────────


async def list_pending_hitl_tool(args: dict, ctx: dict) -> dict:
    """List items pending the lawyer's approval."""
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return _err("firm_id required")

    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, kind, payload, matter_id, created_at, expires_at
            from hitl_interrupts
            where firm_id = $1::uuid and decision = 'pending'
            order by created_at desc
            limit 25
            """,
            firm_id,
        )
    out = []
    for r in rows:
        payload = r["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {"raw": payload}
        out.append({
            "id": str(r["id"]),
            "kind": str(r["kind"]),
            "matter_id": str(r["matter_id"]) if r["matter_id"] else None,
            "preview": payload or {},
            "created_at": r["created_at"].isoformat(),
            "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
        })
    return {"count": len(out), "interrupts": out}


# ─────────────────────────────────────────────────────────────────────
# Firm metrics ("¿cómo voy este mes?")
# ─────────────────────────────────────────────────────────────────────


async def get_firm_metrics_tool(args: dict, ctx: dict) -> dict:
    """Return NSM-style metrics for the active firm (this month + week)."""
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return _err("firm_id required")

    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            with this_month as (
                select coalesce(sum(documentos_verificados),0) as docs,
                       coalesce(sum(voice_commands),0) as voice,
                       coalesce(sum(horas_ahorradas),0)::numeric as horas
                from nsm_daily
                where firm_id = $1::uuid
                  and day >= date_trunc('month', current_date)
            ),
            week as (
                select coalesce(sum(voice_commands),0) as voice_week
                from nsm_daily
                where firm_id = $1::uuid
                  and day >= current_date - interval '7 days'
            ),
            counts as (
                select
                  (select count(*) from matters where firm_id = $1::uuid and status != 'archivado') as matters,
                  (select count(*) from clients where firm_id = $1::uuid) as clients,
                  (select count(*) from hitl_interrupts where firm_id = $1::uuid and decision = 'pending') as hitl,
                  (select count(*) from matter_deadlines
                     where firm_id = $1::uuid and completado = false
                       and fecha <= now() + interval '7 days') as deadlines7d
            )
            select tm.docs, tm.voice, tm.horas, w.voice_week,
                   c.matters, c.clients, c.hitl, c.deadlines7d
            from this_month tm, week w, counts c
            """,
            firm_id,
        )
    return {
        "documentos_mes": int(row["docs"] or 0),
        "voice_commands_mes": int(row["voice"] or 0),
        "voice_commands_semana": int(row["voice_week"] or 0),
        "horas_ahorradas_mes": float(row["horas"] or 0),
        "matters_activos": int(row["matters"] or 0),
        "clientes_total": int(row["clients"] or 0),
        "hitl_pendientes": int(row["hitl"] or 0),
        "deadlines_proximos_7d": int(row["deadlines7d"] or 0),
    }
