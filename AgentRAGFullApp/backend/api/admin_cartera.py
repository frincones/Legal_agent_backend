"""Sprint 24 · SaaS Admin · Cartera (facturas cross-firm).

Endpoints:
  GET   /v1/admin/cartera/overview      · KPIs globales (total cartera, vencido, recaudado)
  GET   /v1/admin/cartera/invoices      · facturas cross-firm con filtros
  POST  /v1/admin/cartera/invoices/{invoice_id}/mark-paid  · forzar marcar pagada
  GET   /v1/admin/cartera/aging         · aging buckets (0-30, 31-60, 61-90, >90)
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from utils.admin_guard import (
    AdminPrincipal, require_saas_admin, require_saas_admin_role, audit_admin_action,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/admin/cartera", tags=["admin-cartera"])


@router.get("/overview")
async def cartera_overview(
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select
              coalesce(sum(case when status in ('sent','overdue','partially_paid')
                            then total_cop - paid_amount_cop else 0 end), 0)::bigint as cartera_total,
              coalesce(sum(case when status = 'overdue' or (status = 'sent' and due_date < current_date)
                            then total_cop - paid_amount_cop else 0 end), 0)::bigint as cartera_vencida,
              coalesce(sum(case when status = 'paid' and paid_at >= date_trunc('month', now())
                            then paid_amount_cop else 0 end), 0)::bigint as recaudado_mtd,
              coalesce(sum(case when sent_at >= date_trunc('month', now())
                            then total_cop else 0 end), 0)::bigint as facturado_mtd,
              count(*) filter (where status in ('sent','overdue','partially_paid')) as facturas_abiertas,
              count(*) filter (where status = 'overdue' or (status = 'sent' and due_date < current_date)) as facturas_vencidas,
              count(*) filter (where status = 'paid' and paid_at >= date_trunc('month', now())) as pagadas_mtd
            from invoices
            """
        )
    return dict(row) if row else {}


@router.get("/invoices")
async def list_invoices(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    status: Optional[str] = None,
    firm_id: Optional[str] = None,
    overdue_only: bool = False,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    where = ["1=1"]
    params: list = []
    if status:
        params.append(status); where.append(f"i.status = ${len(params)}")
    if firm_id:
        params.append(firm_id); where.append(f"i.firm_id = ${len(params)}::uuid")
    if overdue_only:
        where.append("(i.status = 'overdue' or (i.status = 'sent' and i.due_date < current_date))")
    params.extend([limit, offset])
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            select i.id, i.number, i.firm_id, f.razon_social as firm_name,
                   i.client_id, c.nombre as client_name,
                   i.total_cop, i.paid_amount_cop, (i.total_cop - i.paid_amount_cop) as saldo_cop,
                   i.status, i.due_date, i.sent_at, i.paid_at,
                   case when i.due_date is not null and i.due_date < current_date and i.status not in ('paid','void')
                        then current_date - i.due_date else 0 end as days_overdue
              from invoices i
              left join firms f on f.id = i.firm_id
              left join clients c on c.id = i.client_id
             where {' and '.join(where)}
             order by i.sent_at desc nulls last, i.id desc
             limit ${len(params) - 1} offset ${len(params)}
            """, *params,
        )
        total = await conn.fetchval("select count(*) from invoices")
    return {"items": [dict(r) for r in rows], "total": total or 0, "limit": limit, "offset": offset}


class MarkPaidBody(BaseModel):
    amount_cop: Optional[float] = None      # null → full amount
    reason: Optional[str] = None


@router.post("/invoices/{invoice_id}/mark-paid")
async def mark_invoice_paid(
    invoice_id: str,
    body: MarkPaidBody,
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin_role("owner", "admin")),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        inv = await conn.fetchrow(
            "select id, firm_id, total_cop, paid_amount_cop, status from invoices where id = $1::uuid",
            invoice_id,
        )
        if not inv:
            raise HTTPException(404, "invoice not found")
        if inv["status"] == "paid":
            return {"ok": True, "already_paid": True}
        new_paid = float(body.amount_cop) if body.amount_cop is not None else float(inv["total_cop"])
        new_status = "paid" if new_paid >= float(inv["total_cop"]) else "partially_paid"
        await conn.execute(
            """
            update invoices
               set paid_amount_cop = $2,
                   status = $3,
                   paid_at = case when $3 = 'paid' then now() else paid_at end,
                   updated_at = now()
             where id = $1::uuid
            """,
            invoice_id, new_paid, new_status,
        )
    await audit_admin_action(admin, "cartera.mark_paid", resource_type="invoice",
                             resource_id=invoice_id, firm_id=str(inv["firm_id"]), request=request,
                             metadata={"amount_cop": new_paid, "new_status": new_status, "reason": body.reason})
    return {"ok": True, "status": new_status, "paid_amount_cop": new_paid}


@router.get("/aging")
async def cartera_aging(
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            with open_inv as (
              select (total_cop - paid_amount_cop) as saldo,
                     case
                       when due_date is null or due_date >= current_date then 'current'
                       when current_date - due_date <= 30 then 'd_0_30'
                       when current_date - due_date <= 60 then 'd_31_60'
                       when current_date - due_date <= 90 then 'd_61_90'
                       else 'd_over_90'
                     end as bucket
                from invoices
               where status in ('sent','overdue','partially_paid')
            )
            select
              coalesce(sum(saldo) filter (where bucket = 'current'), 0)::bigint as current,
              coalesce(sum(saldo) filter (where bucket = 'd_0_30'),  0)::bigint as d_0_30,
              coalesce(sum(saldo) filter (where bucket = 'd_31_60'), 0)::bigint as d_31_60,
              coalesce(sum(saldo) filter (where bucket = 'd_61_90'), 0)::bigint as d_61_90,
              coalesce(sum(saldo) filter (where bucket = 'd_over_90'), 0)::bigint as d_over_90
              from open_inv
            """
        )
    return dict(row) if row else {}
