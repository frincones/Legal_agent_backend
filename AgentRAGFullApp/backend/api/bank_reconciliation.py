"""Sprint 10 · Bank reconciliation API.

Flujo:
  1. POST /v1/trust/reconciliation/statements        · subir CSV del banco
  2. POST /v1/trust/reconciliation/statements/{id}/auto-match · matchear automáticamente
  3. POST /v1/trust/reconciliation/lines/{id}/match  · match manual
  4. POST /v1/trust/reconciliation/lines/{id}/unmatch
  5. GET  /v1/trust/reconciliation/statements/{id}   · ver estado

Algoritmo auto-match:
  - Match exacto (amount + date dentro de ±2 días) → confidence 1.0
  - Match fuzzy (amount exacto + date ±5 días) → confidence 0.85
  - Match por reference (si línea bancaria tiene número de cheque/transfer) → 0.95
  - Match parcial (description ILIKE description) → 0.6
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/trust/reconciliation", tags=["trust_reconciliation"])


@router.post("/statements")
async def upload_statement(
    trust_account_id: str = Form(...),
    period_start: str = Form(...),
    period_end: str = Form(...),
    file: UploadFile = File(...),
    principal: Principal = Depends(get_current_firm),
):
    """Sube CSV con columnas: fecha,descripcion,monto,referencia (header optional).
    Convención: monto positivo = entrada, negativo = salida."""
    if principal.role not in ("admin", "socio_senior", "socio_junior"):
        raise HTTPException(403, "Solo socios/admin pueden importar extractos")

    raw = (await file.read()).decode("utf-8-sig", errors="ignore")
    if not raw.strip():
        raise HTTPException(400, "archivo vacío")

    # Detectar delimitador
    delim = ","
    if raw.count(";") > raw.count(","):
        delim = ";"

    reader = csv.reader(io.StringIO(raw), delimiter=delim)
    rows = list(reader)
    if not rows:
        raise HTTPException(400, "no rows")

    # Detectar header
    has_header = any(c.lower() in ("fecha", "date", "descripcion", "monto", "amount", "referencia") for c in rows[0])
    data_rows = rows[1:] if has_header else rows

    parsed: list[dict] = []
    for r in data_rows:
        if len(r) < 3:
            continue
        date_str, desc, amount_str = r[0].strip(), r[1].strip(), r[2].strip()
        ref = r[3].strip() if len(r) > 3 else None
        try:
            d = _parse_date(date_str)
            amount = _parse_amount(amount_str)
        except Exception:
            continue
        if d is None or amount is None:
            continue
        parsed.append({"occurred_on": d, "description": desc, "amount": amount, "reference": ref})

    if not parsed:
        raise HTTPException(400, "no se pudo parsear ninguna fila")

    opening = sum(p["amount"] for p in parsed if p["amount"] > 0)
    closing = opening + sum(p["amount"] for p in parsed if p["amount"] < 0)
    total_in = sum(p["amount"] for p in parsed if p["amount"] > 0)
    total_out = abs(sum(p["amount"] for p in parsed if p["amount"] < 0))

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")

    async with storage.pool.acquire() as conn:
        # Validar trust_account pertenece al firm
        acct = await conn.fetchval(
            "select 1 from trust_accounts where id = $1::uuid and firm_id = $2::uuid",
            trust_account_id, principal.firm_id,
        )
        if not acct:
            raise HTTPException(404, "trust_account no encontrada")

        stmt = await conn.fetchrow(
            """
            insert into bank_statements
              (firm_id, trust_account_id, period_start, period_end,
               opening_balance_cop, closing_balance_cop,
               source_filename, source_format, imported_by)
            values ($1::uuid, $2::uuid, $3::date, $4::date,
                    0, 0, $5, 'csv', $6::uuid)
            returning id
            """,
            principal.firm_id, trust_account_id, period_start, period_end,
            file.filename, principal.user_id,
        )
        stmt_id = stmt["id"]

        # Bulk insert lines
        running = 0
        for p in parsed:
            running += p["amount"]
            await conn.execute(
                """
                insert into bank_statement_lines
                  (firm_id, bank_statement_id, occurred_on, amount_cop,
                   description, reference, balance_after_cop)
                values ($1::uuid, $2::uuid, $3::date, $4, $5, $6, $7)
                """,
                principal.firm_id, stmt_id, p["occurred_on"], p["amount"],
                p["description"], p["reference"], running,
            )
        # Update closing
        await conn.execute(
            "update bank_statements set closing_balance_cop = $2 where id = $1::uuid",
            stmt_id, running,
        )

    return {
        "statement_id": str(stmt_id),
        "lines_imported": len(parsed),
        "total_in_cop": float(total_in),
        "total_out_cop": float(total_out),
        "closing_balance_cop": float(running),
    }


def _parse_date(s: str):
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue
    return None


def _parse_amount(s: str):
    s = s.replace("$", "").replace(" ", "").strip()
    # Soportar formato CO: $1.234.567,89 → 1234567.89
    # y formato US: $1,234,567.89 → 1234567.89
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):  # CO style
            s = s.replace(".", "").replace(",", ".")
        else:                                 # US style
            s = s.replace(",", "")
    elif "," in s:
        # solo coma — asumimos decimal CO
        if s.count(",") == 1 and len(s.split(",")[-1]) <= 2:
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    try:
        return float(s)
    except Exception:
        return None


@router.get("/statements")
async def list_statements(
    trust_account_id: Optional[str] = None,
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
    sql = f"""
        select bs.id, bs.trust_account_id, bs.period_start, bs.period_end,
               bs.opening_balance_cop, bs.closing_balance_cop, bs.source_filename,
               bs.imported_at,
               (select count(*) from bank_statement_lines bsl
                  where bsl.bank_statement_id = bs.id) as line_count,
               (select count(*) from bank_statement_lines bsl
                  where bsl.bank_statement_id = bs.id and bsl.matched_transaction_id is null) as unmatched_count
          from bank_statements bs
         where {' and '.join(where)}
         order by bs.imported_at desc
         limit 100
    """
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]),
                "trust_account_id": str(r["trust_account_id"]),
                "period_start": r["period_start"].isoformat() if r["period_start"] else None,
                "period_end": r["period_end"].isoformat() if r["period_end"] else None,
                "opening_balance_cop": float(r["opening_balance_cop"]),
                "closing_balance_cop": float(r["closing_balance_cop"]),
                "source_filename": r["source_filename"],
                "imported_at": r["imported_at"].isoformat() if r["imported_at"] else None,
                "line_count": r["line_count"],
                "unmatched_count": r["unmatched_count"],
            }
            for r in rows
        ],
    }


@router.get("/statements/{statement_id}")
async def get_statement(
    statement_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        stmt = await conn.fetchrow(
            """
            select id, trust_account_id, period_start, period_end,
                   opening_balance_cop, closing_balance_cop, source_filename, imported_at
              from bank_statements
             where id = $1::uuid and firm_id = $2::uuid
            """,
            statement_id, principal.firm_id,
        )
        if not stmt:
            raise HTTPException(404, "not found")
        lines = await conn.fetch(
            """
            select id, occurred_on, amount_cop, description, reference,
                   balance_after_cop, matched_transaction_id, match_confidence, match_method
              from bank_statement_lines
             where bank_statement_id = $1::uuid
             order by occurred_on, id
            """,
            statement_id,
        )
        # Transacciones candidatas para match (no conciliadas, mismo trust account)
        candidates = await conn.fetch(
            """
            select id, occurred_on, amount_cop, direction, description, reference,
                   matter_id, kind
              from trust_transactions
             where trust_account_id = $1::uuid and firm_id = $2::uuid
               and reconciled = false
               and occurred_on between $3::date - 7 and $4::date + 7
             order by occurred_on
            """,
            stmt["trust_account_id"], principal.firm_id,
            stmt["period_start"], stmt["period_end"],
        )
    return {
        "statement": {
            "id": str(stmt["id"]),
            "trust_account_id": str(stmt["trust_account_id"]),
            "period_start": stmt["period_start"].isoformat() if stmt["period_start"] else None,
            "period_end": stmt["period_end"].isoformat() if stmt["period_end"] else None,
            "opening_balance_cop": float(stmt["opening_balance_cop"]),
            "closing_balance_cop": float(stmt["closing_balance_cop"]),
            "source_filename": stmt["source_filename"],
        },
        "lines": [
            {
                "id": str(l["id"]),
                "occurred_on": l["occurred_on"].isoformat() if l["occurred_on"] else None,
                "amount_cop": float(l["amount_cop"]),
                "description": l["description"],
                "reference": l["reference"],
                "balance_after_cop": float(l["balance_after_cop"]) if l["balance_after_cop"] is not None else None,
                "matched_transaction_id": str(l["matched_transaction_id"]) if l["matched_transaction_id"] else None,
                "match_confidence": float(l["match_confidence"]) if l["match_confidence"] is not None else None,
                "match_method": l["match_method"],
            }
            for l in lines
        ],
        "candidates": [
            {
                "id": str(c["id"]),
                "occurred_on": c["occurred_on"].isoformat() if c["occurred_on"] else None,
                "amount_cop": float(c["amount_cop"]),
                "direction": c["direction"],
                "kind": c["kind"],
                "description": c["description"],
                "reference": c["reference"],
                "matter_id": str(c["matter_id"]) if c["matter_id"] else None,
                "signed_amount": float(c["amount_cop"]) if c["direction"] == "in" else -float(c["amount_cop"]),
            }
            for c in candidates
        ],
    }


@router.post("/statements/{statement_id}/auto-match")
async def auto_match(
    statement_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    matched = 0
    async with storage.pool.acquire() as conn:
        stmt = await conn.fetchrow(
            "select trust_account_id from bank_statements where id = $1::uuid and firm_id = $2::uuid",
            statement_id, principal.firm_id,
        )
        if not stmt:
            raise HTTPException(404, "not found")
        lines = await conn.fetch(
            """
            select id, occurred_on, amount_cop, description, reference
              from bank_statement_lines
             where bank_statement_id = $1::uuid and matched_transaction_id is null
            """,
            statement_id,
        )
        for l in lines:
            signed = float(l["amount_cop"])
            abs_amount = abs(signed)
            direction = "in" if signed > 0 else "out"
            # Buscar candidato exact match
            cand = await conn.fetchrow(
                """
                select id from trust_transactions
                 where firm_id = $1::uuid and trust_account_id = $2::uuid
                   and reconciled = false
                   and direction = $3
                   and abs(amount_cop - $4) < 1
                   and occurred_on between $5::date - 2 and $5::date + 2
                 order by abs(occurred_on - $5::date)
                 limit 1
                """,
                principal.firm_id, stmt["trust_account_id"],
                direction, abs_amount, l["occurred_on"],
            )
            confidence = 1.0
            method = "auto_exact"
            if not cand:
                # Fuzzy: same amount ±5 días
                cand = await conn.fetchrow(
                    """
                    select id from trust_transactions
                     where firm_id = $1::uuid and trust_account_id = $2::uuid
                       and reconciled = false and direction = $3
                       and abs(amount_cop - $4) < 1
                       and occurred_on between $5::date - 5 and $5::date + 5
                     limit 1
                    """,
                    principal.firm_id, stmt["trust_account_id"],
                    direction, abs_amount, l["occurred_on"],
                )
                confidence = 0.85
                method = "auto_fuzzy"
            if not cand and l["reference"]:
                # Match por reference
                cand = await conn.fetchrow(
                    """
                    select id from trust_transactions
                     where firm_id = $1::uuid and trust_account_id = $2::uuid
                       and reconciled = false and direction = $3
                       and reference is not null and reference ilike $4
                     limit 1
                    """,
                    principal.firm_id, stmt["trust_account_id"],
                    direction, f"%{l['reference']}%",
                )
                confidence = 0.95
                method = "auto_reference"
            if cand:
                await conn.execute(
                    """
                    update bank_statement_lines set
                      matched_transaction_id = $2::uuid,
                      match_confidence = $3,
                      match_method = $4
                     where id = $1::uuid
                    """,
                    l["id"], cand["id"], confidence, method,
                )
                await conn.execute(
                    """
                    update trust_transactions set
                      reconciled = true, reconciled_with = $2::uuid
                     where id = $1::uuid
                    """,
                    cand["id"], l["id"],
                )
                matched += 1
    return {"statement_id": statement_id, "matched": matched, "total_lines": len(lines)}


class ManualMatchRequest(BaseModel):
    transaction_id: str


@router.post("/lines/{line_id}/match")
async def manual_match(
    line_id: str,
    body: ManualMatchRequest,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        # Validar que ambos sean del firm
        line = await conn.fetchrow(
            "select id, matched_transaction_id from bank_statement_lines where id = $1::uuid and firm_id = $2::uuid",
            line_id, principal.firm_id,
        )
        tx = await conn.fetchrow(
            "select id, reconciled from trust_transactions where id = $1::uuid and firm_id = $2::uuid",
            body.transaction_id, principal.firm_id,
        )
        if not (line and tx):
            raise HTTPException(404, "line o transaction no encontrados")
        if line["matched_transaction_id"]:
            raise HTTPException(409, "línea ya conciliada · desconcilia primero")
        if tx["reconciled"]:
            raise HTTPException(409, "transacción ya conciliada con otra línea")
        await conn.execute(
            """update bank_statement_lines set
                 matched_transaction_id = $2::uuid,
                 match_confidence = 1.0, match_method = 'manual'
               where id = $1::uuid""",
            line_id, body.transaction_id,
        )
        await conn.execute(
            "update trust_transactions set reconciled = true, reconciled_with = $2::uuid where id = $1::uuid",
            body.transaction_id, line_id,
        )
    return {"ok": True}


@router.post("/lines/{line_id}/unmatch")
async def unmatch(
    line_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            "select matched_transaction_id from bank_statement_lines where id = $1::uuid and firm_id = $2::uuid",
            line_id, principal.firm_id,
        )
        if not row or not row["matched_transaction_id"]:
            raise HTTPException(404, "no match")
        await conn.execute(
            """update bank_statement_lines set
                 matched_transaction_id = null, match_confidence = null, match_method = null
               where id = $1::uuid""",
            line_id,
        )
        await conn.execute(
            "update trust_transactions set reconciled = false, reconciled_with = null where id = $1::uuid",
            row["matched_transaction_id"],
        )
    return {"ok": True}
