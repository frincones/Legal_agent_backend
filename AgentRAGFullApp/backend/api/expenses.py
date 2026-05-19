"""Sprint 8 · Expenses API · gastos del caso reembolsables.

  GET    /v1/expenses?matter_id=&since=&until=
  POST   /v1/expenses
  PATCH  /v1/expenses/{id}
  DELETE /v1/expenses/{id}
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/expenses", tags=["expenses"])

EXPENSE_KINDS = {"desplazamiento", "copias", "aranceles", "peritaje", "notariado", "viaticos", "otro"}


def _serialize(r) -> dict:
    return {
        "id": str(r["id"]),
        "matter_id": str(r["matter_id"]),
        "user_id": str(r["user_id"]) if r["user_id"] else None,
        "kind": r["kind"],
        "amount_cop": float(r["amount_cop"]),
        "occurred_on": r["occurred_on"].isoformat() if r["occurred_on"] else None,
        "description": r["description"],
        "billable": r["billable"],
        "receipt_path": r["receipt_path"],
        "invoiced": r["invoice_line_id"] is not None,
    }


@router.get("")
async def list_expenses(
    matter_id: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    only_unbilled: bool = False,
    limit: int = Query(default=100, le=500),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    where = ["firm_id = $1::uuid"]
    params: list = [principal.firm_id]
    if matter_id:
        params.append(matter_id); where.append(f"matter_id = ${len(params)}::uuid")
    if since:
        params.append(since); where.append(f"occurred_on >= ${len(params)}::date")
    if until:
        params.append(until); where.append(f"occurred_on <= ${len(params)}::date")
    if only_unbilled:
        where.append("invoice_line_id is null")
    params.append(limit)
    sql = f"""
        select id, matter_id, user_id, kind, amount_cop, occurred_on,
               description, billable, receipt_path, invoice_line_id
          from expenses
         where {' and '.join(where)}
         order by occurred_on desc, created_at desc
         limit ${len(params)}
    """
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {"count": len(rows), "items": [_serialize(r) for r in rows]}


class CreateRequest(BaseModel):
    matter_id: str
    kind: str = Field(min_length=2)
    amount_cop: float = Field(gt=0)
    occurred_on: Optional[str] = None
    description: str = ""
    billable: bool = True
    receipt_path: Optional[str] = None


@router.post("")
async def create_expense(
    body: CreateRequest,
    principal: Principal = Depends(get_current_firm),
):
    if body.kind not in EXPENSE_KINDS and body.kind != "otro":
        # Permitimos cualquier kind libre, solo loggeamos si es no estándar
        logger.debug("expense kind no estándar: %s", body.kind)
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into expenses
              (firm_id, matter_id, user_id, kind, amount_cop, occurred_on,
               description, billable, receipt_path)
            values ($1::uuid, $2::uuid, $3::uuid, $4, $5, coalesce($6::date, current_date),
                    $7, $8, $9)
            returning id, matter_id, user_id, kind, amount_cop, occurred_on,
                      description, billable, receipt_path, invoice_line_id
            """,
            principal.firm_id, body.matter_id, principal.user_id,
            body.kind, body.amount_cop, body.occurred_on,
            body.description, body.billable, body.receipt_path,
        )
    return _serialize(row)


class PatchRequest(BaseModel):
    kind: Optional[str] = None
    amount_cop: Optional[float] = Field(default=None, gt=0)
    occurred_on: Optional[str] = None
    description: Optional[str] = None
    billable: Optional[bool] = None


@router.patch("/{expense_id}")
async def patch_expense(
    expense_id: str,
    body: PatchRequest,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    fields, params = [], [expense_id, principal.firm_id]
    for f in ("kind", "amount_cop", "occurred_on", "description", "billable"):
        v = getattr(body, f)
        if v is not None:
            params.append(v); fields.append(f"{f} = ${len(params)}")
    if not fields:
        raise HTTPException(400, "nada que actualizar")
    sql = f"""
        update expenses set {', '.join(fields)}
         where id = $1::uuid and firm_id = $2::uuid and invoice_line_id is null
         returning id, matter_id, user_id, kind, amount_cop, occurred_on,
                   description, billable, receipt_path, invoice_line_id
    """
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(sql, *params)
    if not row:
        raise HTTPException(404, "no encontrado o ya facturado")
    return _serialize(row)


@router.delete("/{expense_id}")
async def delete_expense(
    expense_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        n = await conn.execute(
            """
            delete from expenses
             where id = $1::uuid and firm_id = $2::uuid and invoice_line_id is null
            """,
            expense_id, principal.firm_id,
        )
    return {"deleted": n}


# ════════════════════════════════════════════════════════════════════════
# Voice tool
# ════════════════════════════════════════════════════════════════════════


async def log_expense_tool(args: dict, ctx: dict) -> dict:
    """Voice: 'LexAI, anota gasto de cincuenta mil pesos en transporte para el caso de Avianca'."""
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    amount = float(args.get("amount_cop") or args.get("amount") or 0)
    kind = (args.get("kind") or "otro").strip().lower()
    description = (args.get("description") or args.get("body")
                   or args.get("text") or "").strip()
    # Si no llega amount, intenta parsearlo del prompt (helper centralizado).
    if not amount:
        from agent.tools._amount_parser import parse_amount_from_text
        prompt_str = (args.get("prompt") or ctx.get("user_prompt") or "")
        parsed = parse_amount_from_text(prompt_str)
        if parsed:
            amount = parsed
        if not description and prompt_str:
            description = prompt_str[:200]
    if not (firm_id and matter_id and amount > 0):
        return {"error": "firm_id, matter_id, amount_cop>0 requeridos",
                "debug_args": list(args.keys())}
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into expenses (firm_id, matter_id, user_id, kind, amount_cop, description, billable)
            values ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, true)
            returning id, amount_cop, kind
            """,
            firm_id, matter_id, user_id, kind, amount, description,
        )
    from agent.tools._ui_events import ui_data_changed
    return {
        "id": str(row["id"]),
        "amount_cop": float(row["amount_cop"]),
        "kind": row["kind"],
        "matter_id": matter_id,
        "_ui_command": ui_data_changed(
            "expenses", matter_id=matter_id, firm_id=firm_id, op="create",
            extra={"expense_id": str(row["id"])},
        ),
    }
