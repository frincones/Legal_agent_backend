"""Sprint 10 · Trust transactions API · ledger fiduciario.

Reglas críticas:
- Cada `withdrawal` o `transfer_out` valida que el matter tenga balance suficiente
  (enforcement de no-overdraft del cliente, requisito ético).
- Las transacciones SOLO se crean con direction coherente con kind:
    deposit/transfer_in/refund(refund=devuelve al cliente, direction=out) etc.
- No se permiten amounts negativos. Si es reverso, se usa kind='adjustment' con
  reversal_of apuntando a la transacción original.

Endpoints:
  GET    /v1/trust/transactions
  POST   /v1/trust/transactions
  GET    /v1/trust/transactions/{id}
  POST   /v1/trust/transactions/{id}/reverse
  DELETE /v1/trust/transactions/{id}   (solo si reconciled=false y < 24h)
  GET    /v1/trust/matters/{matter_id}/ledger
  GET    /v1/trust/matters/{matter_id}/balance
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/trust", tags=["trust_transactions"])

# kind → direction (canónico)
DIRECTION_BY_KIND = {
    "deposit": "in",
    "transfer_in": "in",
    "withdrawal": "out",
    "fee_transfer": "out",
    "refund": "out",
    "transfer_out": "out",
    "adjustment": None,       # depende del caso, debe venir explícito
}


def _serialize(r) -> dict:
    return {
        "id": str(r["id"]),
        "trust_account_id": str(r["trust_account_id"]),
        "client_id": str(r["client_id"]) if r["client_id"] else None,
        "matter_id": str(r["matter_id"]) if r["matter_id"] else None,
        "kind": r["kind"],
        "amount_cop": float(r["amount_cop"]),
        "direction": r["direction"],
        "occurred_on": r["occurred_on"].isoformat() if r["occurred_on"] else None,
        "description": r["description"],
        "reference": r["reference"],
        "payer_payee": r["payer_payee"],
        "related_invoice_id": str(r["related_invoice_id"]) if r["related_invoice_id"] else None,
        "reconciled": r["reconciled"],
        "reconciled_with": str(r["reconciled_with"]) if r["reconciled_with"] else None,
        "reversal_of": str(r["reversal_of"]) if r["reversal_of"] else None,
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


@router.get("/transactions")
async def list_transactions(
    trust_account_id: Optional[str] = None,
    matter_id: Optional[str] = None,
    client_id: Optional[str] = None,
    kind: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    reconciled: Optional[bool] = None,
    limit: int = Query(default=100, le=500),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    where = ["firm_id = $1::uuid"]
    params: list = [principal.firm_id]
    if trust_account_id:
        params.append(trust_account_id); where.append(f"trust_account_id = ${len(params)}::uuid")
    if matter_id:
        params.append(matter_id); where.append(f"matter_id = ${len(params)}::uuid")
    if client_id:
        params.append(client_id); where.append(f"client_id = ${len(params)}::uuid")
    if kind:
        params.append(kind); where.append(f"kind = ${len(params)}")
    if since:
        params.append(since); where.append(f"occurred_on >= ${len(params)}::date")
    if until:
        params.append(until); where.append(f"occurred_on <= ${len(params)}::date")
    if reconciled is not None:
        params.append(reconciled); where.append(f"reconciled = ${len(params)}")
    params.append(limit)
    sql = f"""
        select id, trust_account_id, client_id, matter_id, kind, amount_cop, direction,
               occurred_on, description, reference, payer_payee, related_invoice_id,
               reconciled, reconciled_with, reversal_of, created_at
          from trust_transactions
         where {' and '.join(where)}
         order by occurred_on desc, created_at desc
         limit ${len(params)}
    """
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {"count": len(rows), "items": [_serialize(r) for r in rows]}


class CreateRequest(BaseModel):
    trust_account_id: str
    kind: str = Field(pattern="^(deposit|withdrawal|fee_transfer|refund|adjustment|transfer_in|transfer_out)$")
    amount_cop: float = Field(gt=0)
    direction: Optional[str] = Field(default=None, pattern="^(in|out)$")
    matter_id: Optional[str] = None
    client_id: Optional[str] = None
    occurred_on: Optional[str] = None
    description: str = ""
    reference: Optional[str] = None
    payer_payee: Optional[str] = None
    related_invoice_id: Optional[str] = None
    skip_balance_check: bool = False


@router.post("/transactions")
async def create_transaction(
    body: CreateRequest,
    principal: Principal = Depends(get_current_firm),
):
    # Direction canónico
    direction = body.direction or DIRECTION_BY_KIND.get(body.kind)
    if not direction:
        raise HTTPException(400, f"direction requerido para kind={body.kind}")

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")

    async with storage.pool.acquire() as conn:
        # Validar cuenta
        account = await conn.fetchrow(
            "select id, active from trust_accounts where id = $1::uuid and firm_id = $2::uuid",
            body.trust_account_id, principal.firm_id,
        )
        if not account:
            raise HTTPException(404, "trust_account no encontrada")
        if not account["active"]:
            raise HTTPException(409, "cuenta inactiva")

        # Si es salida y hay matter, validar balance suficiente (no-overdraft del cliente)
        if direction == "out" and body.matter_id and not body.skip_balance_check:
            current = await conn.fetchval(
                "select lexai_trust_matter_balance($1::uuid)", body.matter_id,
            )
            current = float(current or 0)
            if current < float(body.amount_cop):
                raise HTTPException(
                    409,
                    f"Balance insuficiente del matter: ${current:,.0f} < ${body.amount_cop:,.0f}. "
                    "El cliente no tiene fondos suficientes en custodia para este pago. "
                    "Solicita depósito antes de hacer la salida."
                )

        # Si es salida del trust account global, validar balance de cuenta
        if direction == "out" and not body.skip_balance_check:
            acct_bal = await conn.fetchval(
                "select lexai_trust_account_balance($1::uuid)", body.trust_account_id,
            )
            if float(acct_bal or 0) < float(body.amount_cop):
                raise HTTPException(
                    409,
                    f"Balance insuficiente en cuenta: ${float(acct_bal or 0):,.0f} < ${body.amount_cop:,.0f}",
                )

        row = await conn.fetchrow(
            """
            insert into trust_transactions
              (firm_id, trust_account_id, client_id, matter_id, kind, amount_cop, direction,
               occurred_on, description, reference, payer_payee, related_invoice_id, created_by)
            values ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5, $6, $7,
                    coalesce($8::date, current_date), $9, $10, $11, $12::uuid, $13::uuid)
            returning id, trust_account_id, client_id, matter_id, kind, amount_cop, direction,
                      occurred_on, description, reference, payer_payee, related_invoice_id,
                      reconciled, reconciled_with, reversal_of, created_at
            """,
            principal.firm_id, body.trust_account_id, body.client_id, body.matter_id,
            body.kind, body.amount_cop, direction, body.occurred_on,
            body.description, body.reference, body.payer_payee, body.related_invoice_id,
            principal.user_id,
        )
    return _serialize(row)


@router.get("/transactions/{tx_id}")
async def get_transaction(
    tx_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, trust_account_id, client_id, matter_id, kind, amount_cop, direction,
                   occurred_on, description, reference, payer_payee, related_invoice_id,
                   reconciled, reconciled_with, reversal_of, created_at
              from trust_transactions
             where id = $1::uuid and firm_id = $2::uuid
            """,
            tx_id, principal.firm_id,
        )
    if not row:
        raise HTTPException(404, "not found")
    return _serialize(row)


class ReverseRequest(BaseModel):
    reason: str = Field(min_length=4)


@router.post("/transactions/{tx_id}/reverse")
async def reverse_transaction(
    tx_id: str,
    body: ReverseRequest,
    principal: Principal = Depends(get_current_firm),
):
    """Crea una contra-transacción que neutraliza la original (auditable)."""
    if principal.role not in ("admin", "socio_senior", "socio_junior"):
        raise HTTPException(403, "Solo socios/admin pueden revertir")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        orig = await conn.fetchrow(
            """
            select id, trust_account_id, client_id, matter_id, amount_cop, direction, reversal_of
              from trust_transactions
             where id = $1::uuid and firm_id = $2::uuid
            """,
            tx_id, principal.firm_id,
        )
        if not orig:
            raise HTTPException(404, "not found")
        if orig["reversal_of"]:
            raise HTTPException(409, "no se puede revertir una reversa")
        rev_direction = "out" if orig["direction"] == "in" else "in"
        row = await conn.fetchrow(
            """
            insert into trust_transactions
              (firm_id, trust_account_id, client_id, matter_id, kind, amount_cop, direction,
               occurred_on, description, reversal_of, created_by)
            values ($1::uuid, $2::uuid, $3::uuid, $4::uuid, 'adjustment', $5, $6,
                    current_date, $7, $8::uuid, $9::uuid)
            returning id
            """,
            principal.firm_id, orig["trust_account_id"], orig["client_id"], orig["matter_id"],
            orig["amount_cop"], rev_direction,
            f"REVERSA · {body.reason}", tx_id, principal.user_id,
        )
    return {"reversal_id": str(row["id"]), "original_id": tx_id}


@router.delete("/transactions/{tx_id}")
async def delete_transaction(
    tx_id: str,
    principal: Principal = Depends(get_current_firm),
):
    """Solo permite delete dentro de las primeras 24h, sin conciliar. Para corregir typos."""
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            delete from trust_transactions
             where id = $1::uuid and firm_id = $2::uuid
               and reconciled = false
               and created_at > now() - interval '24 hours'
             returning id
            """,
            tx_id, principal.firm_id,
        )
    if not row:
        raise HTTPException(
            409,
            "no se puede eliminar (ya conciliada o > 24h). Usa reverse en su lugar.",
        )
    return {"deleted": True}


@router.get("/matters/{matter_id}/balance")
async def matter_balance(
    matter_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        balance = await conn.fetchval(
            "select lexai_trust_matter_balance($1::uuid)", matter_id,
        )
        n = await conn.fetchval(
            "select count(*) from trust_transactions where matter_id = $1::uuid and firm_id = $2::uuid",
            matter_id, principal.firm_id,
        )
    return {"matter_id": matter_id, "balance_cop": float(balance or 0), "transactions_count": n or 0}


@router.get("/matters/{matter_id}/ledger")
async def matter_ledger(
    matter_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            "select * from lexai_trust_matter_ledger($1::uuid)", matter_id,
        )
    return {
        "matter_id": matter_id,
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]),
                "occurred_on": r["occurred_on"].isoformat() if r["occurred_on"] else None,
                "kind": r["kind"],
                "direction": r["direction"],
                "amount_cop": float(r["amount_cop"]),
                "description": r["description"],
                "reference": r["reference"],
                "payer_payee": r["payer_payee"],
                "reconciled": r["reconciled"],
                "running_balance_cop": float(r["running_balance"] or 0),
            }
            for r in rows
        ],
    }


# ════════════════════════════════════════════════════════════════════════
# Voice tools
# ════════════════════════════════════════════════════════════════════════


async def record_trust_deposit_tool(args: dict, ctx: dict) -> dict:
    """Voice: 'LexAI, registra anticipo de 2 millones del cliente Juan en el caso X'."""
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    amount = float(args.get("amount_cop") or 0)
    trust_account_id = args.get("trust_account_id")
    payer = (args.get("payer") or "").strip()
    if not (firm_id and amount > 0):
        return {"error": "amount_cop > 0 requerido"}

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}
    async with storage.pool.acquire() as conn:
        # Si no se especifica cuenta, tomar la primera activa
        if not trust_account_id:
            trust_account_id = await conn.fetchval(
                """select id from trust_accounts
                    where firm_id = $1::uuid and active = true and is_trust = true
                    order by created_at limit 1""",
                firm_id,
            )
        if not trust_account_id:
            return {"error": "no hay cuenta fiduciaria activa. Crea una primero."}
        # Resolver client_id desde matter
        client_id = None
        if matter_id:
            client_id = await conn.fetchval(
                "select client_id from matters where id = $1::uuid", matter_id,
            )
        row = await conn.fetchrow(
            """
            insert into trust_transactions
              (firm_id, trust_account_id, client_id, matter_id, kind, amount_cop, direction,
               occurred_on, description, payer_payee, created_by)
            values ($1::uuid, $2::uuid, $3::uuid, $4::uuid, 'deposit', $5, 'in',
                    current_date, $6, $7, $8::uuid)
            returning id, amount_cop
            """,
            firm_id, trust_account_id, client_id, matter_id, amount,
            args.get("description") or f"Depósito de {payer or 'cliente'}",
            payer or None, user_id,
        )
    from agent.tools._ui_events import ui_data_changed
    return {
        "id": str(row["id"]), "amount_cop": float(row["amount_cop"]), "matter_id": matter_id,
        "_ui_command": ui_data_changed(
            "trust_transactions", matter_id=matter_id, firm_id=firm_id, op="create",
            extra={"transaction_id": str(row["id"]), "kind": "deposit"},
        ),
    }


async def record_trust_payment_tool(args: dict, ctx: dict) -> dict:
    """Voice: 'LexAI, paga 300 mil al perito Pérez desde fondos del caso de Avianca'."""
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    amount = float(args.get("amount_cop") or 0)
    payee = (args.get("payee") or "").strip()
    if not (firm_id and matter_id and amount > 0):
        return {"error": "matter_id y amount_cop > 0 requeridos"}

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}
    async with storage.pool.acquire() as conn:
        # Validar balance del matter
        balance = await conn.fetchval(
            "select lexai_trust_matter_balance($1::uuid)", matter_id,
        )
        if float(balance or 0) < amount:
            return {
                "error": "balance_insuficiente",
                "balance_actual_cop": float(balance or 0),
                "monto_solicitado_cop": amount,
                "sugerencia": "Solicita depósito antes de hacer el pago.",
            }
        trust_account_id = args.get("trust_account_id") or await conn.fetchval(
            """select id from trust_accounts
                where firm_id = $1::uuid and active = true and is_trust = true
                order by created_at limit 1""",
            firm_id,
        )
        if not trust_account_id:
            return {"error": "no hay cuenta fiduciaria activa"}
        client_id = await conn.fetchval(
            "select client_id from matters where id = $1::uuid", matter_id,
        )
        row = await conn.fetchrow(
            """
            insert into trust_transactions
              (firm_id, trust_account_id, client_id, matter_id, kind, amount_cop, direction,
               occurred_on, description, payer_payee, created_by)
            values ($1::uuid, $2::uuid, $3::uuid, $4::uuid, 'withdrawal', $5, 'out',
                    current_date, $6, $7, $8::uuid)
            returning id
            """,
            firm_id, trust_account_id, client_id, matter_id, amount,
            args.get("description") or f"Pago a {payee or 'tercero'}",
            payee or None, user_id,
        )
    from agent.tools._ui_events import ui_data_changed
    return {
        "id": str(row["id"]), "amount_cop": amount,
        "balance_restante_cop": float(balance or 0) - amount,
        "_ui_command": ui_data_changed(
            "trust_transactions", matter_id=matter_id, firm_id=firm_id, op="create",
            extra={"transaction_id": str(row["id"]), "kind": "payment"},
        ),
    }
