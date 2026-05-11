"""Sprint 8 · Invoices API.

  GET    /v1/invoices?status=&client_id=&matter_id=&limit=
  GET    /v1/invoices/{id}                            · cabecera + líneas
  POST   /v1/invoices/preview                         · simula líneas (sin escribir)
  POST   /v1/invoices                                 · crea (draft) desde matter+período
  POST   /v1/invoices/{id}/finalize                   · draft → sent (lock líneas)
  POST   /v1/invoices/{id}/mark-paid                  · sent → paid
  POST   /v1/invoices/{id}/void                       · void (no se puede si paid)
  DELETE /v1/invoices/{id}                            · solo draft

Flujo:
  1. POST /preview { matter_id, since, until } → cliente ve qué se va a facturar
  2. POST /invoices { matter_id, since, until, tax_pct } → crea draft con líneas
     y MARCA time_entries / expenses con invoice_line_id (lock)
  3. POST /finalize → status='sent', sent_at=now
  4. POST /mark-paid → status='paid', paid_at=now, paid_amount_cop=total
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/invoices", tags=["invoices"])

ADMIN_INVOICE_ROLES = {"admin", "socio_senior", "socio_junior", "lawyer", "paralegal"}


def _serialize_invoice(r) -> dict:
    return {
        "id": str(r["id"]),
        "client_id": str(r["client_id"]) if r["client_id"] else None,
        "matter_id": str(r["matter_id"]) if r["matter_id"] else None,
        "number": r["number"],
        "period_start": r["period_start"].isoformat() if r["period_start"] else None,
        "period_end": r["period_end"].isoformat() if r["period_end"] else None,
        "subtotal_cop": float(r["subtotal_cop"]),
        "tax_pct": float(r["tax_pct"]),
        "tax_cop": float(r["tax_cop"]),
        "retencion_cop": float(r["retencion_cop"]),
        "total_cop": float(r["total_cop"]),
        "currency": r["currency"],
        "status": r["status"],
        "due_date": r["due_date"].isoformat() if r["due_date"] else None,
        "sent_at": r["sent_at"].isoformat() if r["sent_at"] else None,
        "paid_at": r["paid_at"].isoformat() if r["paid_at"] else None,
        "paid_amount_cop": float(r["paid_amount_cop"]),
        "notes": r["notes"],
        "pdf_url": r["pdf_url"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


def _serialize_line(r) -> dict:
    return {
        "id": str(r["id"]),
        "kind": r["kind"],
        "description": r["description"],
        "qty": float(r["qty"]),
        "unit_price_cop": float(r["unit_price_cop"]),
        "total_cop": float(r["total_cop"]),
        "time_entry_id": str(r["time_entry_id"]) if r["time_entry_id"] else None,
        "expense_id": str(r["expense_id"]) if r["expense_id"] else None,
        "position": r["position"],
    }


# ──────────────────────────────────────────────────────────────────────
# Read endpoints
# ──────────────────────────────────────────────────────────────────────


@router.get("")
async def list_invoices(
    status: Optional[str] = Query(default=None, regex="^(draft|sent|paid|partially_paid|void|overdue)$"),
    client_id: Optional[str] = None,
    matter_id: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    where = ["firm_id = $1::uuid"]
    params: list = [principal.firm_id]
    if status:
        params.append(status); where.append(f"status = ${len(params)}")
    if client_id:
        params.append(client_id); where.append(f"client_id = ${len(params)}::uuid")
    if matter_id:
        params.append(matter_id); where.append(f"matter_id = ${len(params)}::uuid")
    params.append(limit)
    sql = f"""
        select id, client_id, matter_id, number, period_start, period_end,
               subtotal_cop, tax_pct, tax_cop, retencion_cop, total_cop, currency,
               status, due_date, sent_at, paid_at, paid_amount_cop, notes, pdf_url, created_at
          from invoices
         where {' and '.join(where)}
         order by created_at desc
         limit ${len(params)}
    """
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {"count": len(rows), "items": [_serialize_invoice(r) for r in rows]}


@router.get("/{invoice_id}")
async def get_invoice(
    invoice_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        inv = await conn.fetchrow(
            """
            select id, client_id, matter_id, number, period_start, period_end,
                   subtotal_cop, tax_pct, tax_cop, retencion_cop, total_cop, currency,
                   status, due_date, sent_at, paid_at, paid_amount_cop, notes, pdf_url, created_at
              from invoices
             where id = $1::uuid and firm_id = $2::uuid
            """,
            invoice_id, principal.firm_id,
        )
        if not inv:
            raise HTTPException(404, "not found")
        lines = await conn.fetch(
            """
            select id, kind, description, qty, unit_price_cop, total_cop,
                   time_entry_id, expense_id, position
              from invoice_lines
             where invoice_id = $1::uuid and firm_id = $2::uuid
             order by position asc
            """,
            invoice_id, principal.firm_id,
        )
    return {"invoice": _serialize_invoice(inv), "lines": [_serialize_line(l) for l in lines]}


# ──────────────────────────────────────────────────────────────────────
# Preview + Create
# ──────────────────────────────────────────────────────────────────────


class PreviewRequest(BaseModel):
    matter_id: str
    since: Optional[str] = None
    until: Optional[str] = None


@router.post("/preview")
async def preview_invoice(
    body: PreviewRequest,
    principal: Principal = Depends(get_current_firm),
):
    """Devuelve líneas tentativas sin escribir nada."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        # Tarifa por defecto (la más reciente sin user_id)
        default_rate = await conn.fetchval(
            """
            select rate_cop from matter_hourly_rates
             where firm_id = $1::uuid and matter_id = $2::uuid and user_id is null
             order by effective_from desc limit 1
            """,
            principal.firm_id, body.matter_id,
        ) or 0
        # Tarifas por usuario
        user_rates = await conn.fetch(
            """
            select user_id, rate_cop from matter_hourly_rates
             where firm_id = $1::uuid and matter_id = $2::uuid and user_id is not null
             order by effective_from desc
            """,
            principal.firm_id, body.matter_id,
        )
        rate_by_user = {}
        for r in user_rates:
            uid = str(r["user_id"])
            if uid not in rate_by_user:
                rate_by_user[uid] = float(r["rate_cop"])
        # Time entries no facturadas
        params: list = [principal.firm_id, body.matter_id]
        where = ["t.firm_id = $1::uuid", "t.matter_id = $2::uuid",
                 "t.invoice_line_id is null", "t.ended_at is not null", "t.billable = true"]
        if body.since:
            params.append(body.since); where.append(f"t.started_at >= ${len(params)}::timestamptz")
        if body.until:
            params.append(body.until); where.append(f"t.started_at <= ${len(params)}::timestamptz")
        time_rows = await conn.fetch(
            f"""
            select t.id, t.user_id, t.duration_min, t.rate_cop, t.description, t.started_at,
                   u.full_name
              from time_entries t left join users u on u.id = t.user_id
             where {' and '.join(where)}
             order by t.started_at asc
            """,
            *params,
        )
        # Expenses
        params2: list = [principal.firm_id, body.matter_id]
        where2 = ["firm_id = $1::uuid", "matter_id = $2::uuid",
                  "invoice_line_id is null", "billable = true"]
        if body.since:
            params2.append(body.since); where2.append(f"occurred_on >= ${len(params2)}::date")
        if body.until:
            params2.append(body.until); where2.append(f"occurred_on <= ${len(params2)}::date")
        expense_rows = await conn.fetch(
            f"""
            select id, kind, amount_cop, occurred_on, description
              from expenses
             where {' and '.join(where2)}
             order by occurred_on asc
            """,
            *params2,
        )

    lines: list[dict] = []
    pos = 0
    for t in time_rows:
        rate = float(t["rate_cop"] or rate_by_user.get(str(t["user_id"]), default_rate) or 0)
        qty = round((t["duration_min"] or 0) / 60.0, 2)
        total = round(qty * rate, 2)
        lines.append({
            "kind": "time",
            "description": (f"{t['full_name'] or 'Abogado'} — " if t['full_name'] else '') + (t["description"] or 'Trabajo profesional'),
            "qty": qty,
            "unit_price_cop": rate,
            "total_cop": total,
            "time_entry_id": str(t["id"]),
            "expense_id": None,
            "position": pos,
        })
        pos += 1
    for e in expense_rows:
        lines.append({
            "kind": "expense",
            "description": f"[{e['kind']}] {e['description'] or e['kind']} ({e['occurred_on']})",
            "qty": 1,
            "unit_price_cop": float(e["amount_cop"]),
            "total_cop": float(e["amount_cop"]),
            "time_entry_id": None,
            "expense_id": str(e["id"]),
            "position": pos,
        })
        pos += 1
    subtotal = round(sum(l["total_cop"] for l in lines), 2)
    return {
        "lines": lines,
        "subtotal_cop": subtotal,
        "time_entries_count": len(time_rows),
        "expenses_count": len(expense_rows),
    }


class CreateRequest(BaseModel):
    matter_id: str
    since: Optional[str] = None
    until: Optional[str] = None
    tax_pct: float = Field(default=19.0, ge=0, le=50)
    retencion_cop: float = Field(default=0, ge=0)
    due_date: Optional[str] = None
    notes: Optional[str] = None


@router.post("")
async def create_invoice(
    body: CreateRequest,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ADMIN_INVOICE_ROLES:
        raise HTTPException(403, "Tu rol no puede crear facturas")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")

    preview = await preview_invoice(PreviewRequest(matter_id=body.matter_id, since=body.since, until=body.until), principal)
    if not preview["lines"]:
        raise HTTPException(400, "No hay horas ni gastos facturables en el periodo")

    async with storage.pool.acquire() as conn:
        matter = await conn.fetchrow(
            "select client_id from matters where id = $1::uuid and firm_id = $2::uuid",
            body.matter_id, principal.firm_id,
        )
        if not matter:
            raise HTTPException(404, "matter no encontrado")
        client_id = matter["client_id"]
        number = await conn.fetchval(
            "select lexai_next_invoice_number($1::uuid)", principal.firm_id,
        )
        subtotal = preview["subtotal_cop"]
        tax = round(subtotal * body.tax_pct / 100, 2)
        total = round(subtotal + tax - body.retencion_cop, 2)
        inv = await conn.fetchrow(
            """
            insert into invoices
              (firm_id, client_id, matter_id, number, period_start, period_end,
               subtotal_cop, tax_pct, tax_cop, retencion_cop, total_cop, currency,
               status, due_date, notes, created_by)
            values ($1::uuid, $2::uuid, $3::uuid, $4, $5::date, $6::date,
                    $7, $8, $9, $10, $11, 'COP', 'draft', $12::date, $13, $14::uuid)
            returning id, client_id, matter_id, number, period_start, period_end,
                      subtotal_cop, tax_pct, tax_cop, retencion_cop, total_cop, currency,
                      status, due_date, sent_at, paid_at, paid_amount_cop, notes, pdf_url, created_at
            """,
            principal.firm_id, client_id, body.matter_id, number,
            body.since, body.until,
            subtotal, body.tax_pct, tax, body.retencion_cop, total,
            body.due_date, body.notes, principal.user_id,
        )
        invoice_id = inv["id"]
        for line in preview["lines"]:
            line_row = await conn.fetchrow(
                """
                insert into invoice_lines
                  (firm_id, invoice_id, kind, description, qty, unit_price_cop, total_cop,
                   time_entry_id, expense_id, position)
                values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7,
                        $8::uuid, $9::uuid, $10)
                returning id
                """,
                principal.firm_id, invoice_id, line["kind"], line["description"],
                line["qty"], line["unit_price_cop"], line["total_cop"],
                line.get("time_entry_id"), line.get("expense_id"), line["position"],
            )
            line_id = line_row["id"]
            if line.get("time_entry_id"):
                await conn.execute(
                    "update time_entries set invoice_line_id = $1::uuid where id = $2::uuid",
                    line_id, line["time_entry_id"],
                )
            elif line.get("expense_id"):
                await conn.execute(
                    "update expenses set invoice_line_id = $1::uuid where id = $2::uuid",
                    line_id, line["expense_id"],
                )

    return _serialize_invoice(inv)


# ──────────────────────────────────────────────────────────────────────
# State transitions
# ──────────────────────────────────────────────────────────────────────


@router.post("/{invoice_id}/finalize")
async def finalize(
    invoice_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update invoices set status = 'sent', sent_at = now(), updated_at = now()
             where id = $1::uuid and firm_id = $2::uuid and status = 'draft'
            returning id, status, sent_at
            """,
            invoice_id, principal.firm_id,
        )
    if not row:
        raise HTTPException(409, "no se puede finalizar (no existe o no es draft)")
    return {"id": str(row["id"]), "status": row["status"], "sent_at": row["sent_at"].isoformat()}


class MarkPaidRequest(BaseModel):
    paid_amount_cop: Optional[float] = None
    paid_at: Optional[str] = None


@router.post("/{invoice_id}/mark-paid")
async def mark_paid(
    invoice_id: str,
    body: MarkPaidRequest,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        inv = await conn.fetchrow(
            "select total_cop from invoices where id = $1::uuid and firm_id = $2::uuid",
            invoice_id, principal.firm_id,
        )
        if not inv:
            raise HTTPException(404, "not found")
        paid = body.paid_amount_cop if body.paid_amount_cop is not None else float(inv["total_cop"])
        status = "paid" if paid >= float(inv["total_cop"]) else "partially_paid"
        row = await conn.fetchrow(
            """
            update invoices set status = $3,
                                paid_at = coalesce($4::timestamptz, now()),
                                paid_amount_cop = $5, updated_at = now()
             where id = $1::uuid and firm_id = $2::uuid
            returning id, status, paid_at, paid_amount_cop
            """,
            invoice_id, principal.firm_id, status, body.paid_at, paid,
        )
    return {
        "id": str(row["id"]),
        "status": row["status"],
        "paid_at": row["paid_at"].isoformat() if row["paid_at"] else None,
        "paid_amount_cop": float(row["paid_amount_cop"]),
    }


@router.post("/{invoice_id}/void")
async def void_invoice(
    invoice_id: str,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        # Liberar time_entries y expenses
        await conn.execute(
            """update time_entries set invoice_line_id = null
                where invoice_line_id in (select id from invoice_lines where invoice_id = $1::uuid)""",
            invoice_id,
        )
        await conn.execute(
            """update expenses set invoice_line_id = null
                where invoice_line_id in (select id from invoice_lines where invoice_id = $1::uuid)""",
            invoice_id,
        )
        row = await conn.fetchrow(
            """
            update invoices set status = 'void', updated_at = now()
             where id = $1::uuid and firm_id = $2::uuid and status <> 'paid'
            returning id, status
            """,
            invoice_id, principal.firm_id,
        )
    if not row:
        raise HTTPException(409, "no se puede anular (no existe o ya pagada)")
    return {"id": str(row["id"]), "status": row["status"]}


@router.delete("/{invoice_id}")
async def delete_invoice(
    invoice_id: str,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        inv = await conn.fetchrow(
            "select status from invoices where id = $1::uuid and firm_id = $2::uuid",
            invoice_id, principal.firm_id,
        )
        if not inv:
            raise HTTPException(404, "not found")
        if inv["status"] != "draft":
            raise HTTPException(409, "solo borradores pueden eliminarse")
        # Liberar líneas
        await conn.execute(
            """update time_entries set invoice_line_id = null
                where invoice_line_id in (select id from invoice_lines where invoice_id = $1::uuid)""",
            invoice_id,
        )
        await conn.execute(
            """update expenses set invoice_line_id = null
                where invoice_line_id in (select id from invoice_lines where invoice_id = $1::uuid)""",
            invoice_id,
        )
        await conn.execute(
            "delete from invoices where id = $1::uuid and firm_id = $2::uuid",
            invoice_id, principal.firm_id,
        )
    return {"deleted": True}


# ════════════════════════════════════════════════════════════════════════
# Voice tool
# ════════════════════════════════════════════════════════════════════════


async def generate_invoice_tool(args: dict, ctx: dict) -> dict:
    """Voice: 'LexAI, factura todas las horas del mes en el caso de Avianca'."""
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    since = args.get("since")
    until = args.get("until")
    tax_pct = float(args.get("tax_pct") or 19.0)
    if not (firm_id and matter_id):
        return {"error": "firm_id, matter_id requeridos"}
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}
    async with storage.pool.acquire() as conn:
        preview = await conn.fetchval(
            "select lexai_billable_summary($1::uuid, $2::uuid, $3::date, $4::date)",
            firm_id, matter_id, since, until,
        )
        preview = preview or {}
        if (preview.get("time_entries", 0) + preview.get("expense_count", 0)) == 0:
            return {"error": "No hay horas ni gastos facturables en el periodo", "preview": preview}
        # Crear via lógica de create_invoice (proxy a la función interna no es trivial sin Request,
        # así que delegamos via HTTP-style approach: replicamos la lógica)
    # Llamamos al endpoint vía función directa
    class _P:
        firm_id = firm_id
        user_id = user_id
        role = ctx.get("role") or "admin"
    inv = await create_invoice(
        CreateRequest(matter_id=matter_id, since=since, until=until, tax_pct=tax_pct),
        principal=_P(),  # type: ignore
    )
    return {
        "invoice_id": inv["id"],
        "number": inv["number"],
        "total_cop": inv["total_cop"],
        "status": inv["status"],
    }
