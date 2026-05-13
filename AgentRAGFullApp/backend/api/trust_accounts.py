"""Sprint 10 · Trust Accounts API.

Cuentas bancarias fiduciarias de la firma para manejar fondos de clientes
segregados (Ley 1123/2007 - Código Disciplinario del Abogado CO).

  GET    /v1/trust/accounts
  POST   /v1/trust/accounts
  GET    /v1/trust/accounts/{id}              · con balance vigente
  PATCH  /v1/trust/accounts/{id}
  DELETE /v1/trust/accounts/{id}              · solo si sin transacciones
  GET    /v1/trust/summary                    · KPIs globales
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/trust", tags=["trust_accounts"])

ADMIN_ROLES = {"admin", "socio_senior", "socio_junior"}


def _serialize(r) -> dict:
    return {
        "id": str(r["id"]),
        "name": r["name"],
        "bank_name": r["bank_name"],
        "account_number": r["account_number"],
        "account_type": r["account_type"],
        "currency": r["currency"],
        "is_trust": r["is_trust"],
        "active": r["active"],
        "notes": r["notes"],
        "opening_balance_cop": float(r["opening_balance_cop"]) if r["opening_balance_cop"] is not None else 0,
        "opening_date": r["opening_date"].isoformat() if r["opening_date"] else None,
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


@router.get("/accounts")
async def list_accounts(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, name, bank_name, account_number, account_type, currency,
                   is_trust, active, notes, opening_balance_cop, opening_date, created_at
              from trust_accounts
             where firm_id = $1::uuid
             order by active desc, created_at desc
            """,
            principal.firm_id,
        )
        # balance vigente por cuenta
        balances = {}
        for r in rows:
            b = await conn.fetchval(
                "select lexai_trust_account_balance($1::uuid)", r["id"],
            )
            balances[str(r["id"])] = float(b or 0)
    return {
        "count": len(rows),
        "items": [{**_serialize(r), "balance_cop": balances[str(r["id"])]} for r in rows],
    }


class CreateRequest(BaseModel):
    name: str = Field(min_length=2)
    bank_name: str = Field(min_length=2)
    account_number: str = Field(min_length=4)
    account_type: str = Field(default="corriente", pattern="^(corriente|ahorros|escrow)$")
    currency: str = Field(default="COP")
    is_trust: bool = True
    opening_balance_cop: float = Field(default=0, ge=0)
    opening_date: Optional[str] = None
    notes: Optional[str] = None


@router.post("/accounts")
async def create_account(
    body: CreateRequest,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ADMIN_ROLES:
        raise HTTPException(403, "Solo socios/admin pueden crear cuentas fiduciarias")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                insert into trust_accounts
                  (firm_id, name, bank_name, account_number, account_type, currency,
                   is_trust, opening_balance_cop, opening_date, notes, created_by)
                values ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9::date, $10, $11::uuid)
                returning id, name, bank_name, account_number, account_type, currency,
                          is_trust, active, notes, opening_balance_cop, opening_date, created_at
                """,
                principal.firm_id, body.name, body.bank_name, body.account_number,
                body.account_type, body.currency, body.is_trust,
                body.opening_balance_cop, body.opening_date, body.notes, principal.user_id,
            )
        except Exception as e:
            if "unique" in str(e).lower():
                raise HTTPException(409, "ya existe una cuenta con ese número")
            raise
    return {**_serialize(row), "balance_cop": float(body.opening_balance_cop)}


@router.get("/accounts/{account_id}")
async def get_account(
    account_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, name, bank_name, account_number, account_type, currency,
                   is_trust, active, notes, opening_balance_cop, opening_date, created_at
              from trust_accounts
             where id = $1::uuid and firm_id = $2::uuid
            """,
            account_id, principal.firm_id,
        )
        if not row:
            raise HTTPException(404, "not found")
        balance = await conn.fetchval(
            "select lexai_trust_account_balance($1::uuid)", account_id,
        )
    return {**_serialize(row), "balance_cop": float(balance or 0)}


class PatchRequest(BaseModel):
    name: Optional[str] = None
    bank_name: Optional[str] = None
    account_type: Optional[str] = Field(default=None, pattern="^(corriente|ahorros|escrow)$")
    active: Optional[bool] = None
    notes: Optional[str] = None


@router.patch("/accounts/{account_id}")
async def patch_account(
    account_id: str,
    body: PatchRequest,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ADMIN_ROLES:
        raise HTTPException(403, "Solo socios/admin")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    fields, params = [], [account_id, principal.firm_id]
    for f in ("name", "bank_name", "account_type", "active", "notes"):
        v = getattr(body, f)
        if v is not None:
            params.append(v); fields.append(f"{f} = ${len(params)}")
    if not fields:
        raise HTTPException(400, "nada que actualizar")
    sql = f"""
        update trust_accounts set {', '.join(fields)}, updated_at = now()
         where id = $1::uuid and firm_id = $2::uuid
         returning id, name, bank_name, account_number, account_type, currency,
                   is_trust, active, notes, opening_balance_cop, opening_date, created_at
    """
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(sql, *params)
    if not row:
        raise HTTPException(404, "not found")
    return _serialize(row)


@router.delete("/accounts/{account_id}")
async def delete_account(
    account_id: str,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        n = await conn.fetchval(
            "select count(*) from trust_transactions where trust_account_id = $1::uuid",
            account_id,
        )
        if (n or 0) > 0:
            raise HTTPException(
                409,
                f"hay {n} transacciones en esta cuenta; márcala como inactiva en su lugar",
            )
        await conn.execute(
            "delete from trust_accounts where id = $1::uuid and firm_id = $2::uuid",
            account_id, principal.firm_id,
        )
    return {"deleted": True}


@router.get("/summary")
async def summary(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        result = await conn.fetchval(
            "select lexai_trust_summary($1::uuid)", principal.firm_id,
        )
    return result or {}


# ════════════════════════════════════════════════════════════════════════
# Voice tool
# ════════════════════════════════════════════════════════════════════════


async def check_trust_balance_tool(args: dict, ctx: dict) -> dict:
    """Voice: 'LexAI, ¿cuánto tengo del cliente Juan en custodia?'."""
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}
    async with storage.pool.acquire() as conn:
        if matter_id:
            balance = await conn.fetchval(
                "select lexai_trust_matter_balance($1::uuid)", matter_id,
            )
            return {"matter_id": matter_id, "balance_cop": float(balance or 0)}
        # Si no, devuelve el resumen general
        summary = await conn.fetchval(
            "select lexai_trust_summary($1::uuid)", firm_id,
        )
    return summary or {}
