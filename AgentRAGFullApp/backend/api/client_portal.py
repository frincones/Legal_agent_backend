"""Sprint 8 · Client Portal API (magic-link auth).

Endpoints internos (firma autenticada):
  POST   /v1/client-portal/tokens                  · genera magic link para un client
  GET    /v1/client-portal/tokens?client_id=...    · lista tokens activos
  DELETE /v1/client-portal/tokens/{id}             · revoca

Endpoints públicos (token en query, sin Supabase auth):
  GET    /v1/portal/{token}                        · dashboard del cliente
  GET    /v1/portal/{token}/matters
  GET    /v1/portal/{token}/matters/{matter_id}
  GET    /v1/portal/{token}/invoices
  GET    /v1/portal/{token}/invoices/{invoice_id}

Seguridad:
  - Tokens random 32-byte hex (no JWT, no Supabase session)
  - Expires_at obligatorio (default 30 días)
  - revoked_at desactiva inmediatamente
  - Validamos firm_id desde el token, NUNCA permitimos sobrescribir desde URL
  - Solo lectura · cliente NUNCA modifica datos
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router_admin = APIRouter(prefix="/v1/client-portal", tags=["client_portal_admin"])
router_public = APIRouter(prefix="/v1/portal", tags=["client_portal_public"])


# ──────────────────────────────────────────────────────────────────────
# Admin · gestión de tokens
# ──────────────────────────────────────────────────────────────────────


class CreateTokenRequest(BaseModel):
    client_id: str
    ttl_days: int = Field(default=30, ge=1, le=365)
    scope: list[str] = Field(default_factory=lambda: ["matters", "invoices", "documents"])


@router_admin.post("/tokens")
async def create_token(
    body: CreateTokenRequest,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior", "socio_junior", "lawyer"):
        raise HTTPException(403, "Tu rol no puede emitir portal tokens")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    raw = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=body.ttl_days)
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into client_portal_tokens
              (firm_id, client_id, token, scope, expires_at, created_by)
            values ($1::uuid, $2::uuid, $3, $4::text[], $5::timestamptz, $6::uuid)
            returning id, token, expires_at
            """,
            principal.firm_id, body.client_id, raw, body.scope, expires, principal.user_id,
        )
    return {
        "id": str(row["id"]),
        "token": row["token"],
        "expires_at": row["expires_at"].isoformat(),
        "ttl_days": body.ttl_days,
        "portal_url_hint": f"/portal/{row['token']}",
    }


@router_admin.get("/tokens")
async def list_tokens(
    client_id: Optional[str] = None,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    where = ["firm_id = $1::uuid"]
    params: list = [principal.firm_id]
    if client_id:
        params.append(client_id); where.append(f"client_id = ${len(params)}::uuid")
    sql = f"""
        select id, client_id, token, scope, expires_at, last_used_at, use_count, revoked_at, created_at
          from client_portal_tokens
         where {' and '.join(where)}
         order by created_at desc
         limit 100
    """
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]),
                "client_id": str(r["client_id"]),
                "token_preview": (r["token"][:8] + "…") if r["token"] else None,
                "scope": r["scope"],
                "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
                "last_used_at": r["last_used_at"].isoformat() if r["last_used_at"] else None,
                "use_count": r["use_count"],
                "revoked": r["revoked_at"] is not None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


@router_admin.delete("/tokens/{token_id}")
async def revoke_token(
    token_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            update client_portal_tokens set revoked_at = now()
             where id = $1::uuid and firm_id = $2::uuid
            """,
            token_id, principal.firm_id,
        )
    return {"revoked": True}


# ──────────────────────────────────────────────────────────────────────
# Public · vistas del cliente
# ──────────────────────────────────────────────────────────────────────


async def _resolve_token(token: str) -> dict:
    """Valida token y devuelve {firm_id, client_id, scope}. Lanza HTTPException si inválido."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, firm_id, client_id, scope, expires_at, revoked_at
              from client_portal_tokens
             where token = $1
            """,
            token,
        )
        if not row:
            raise HTTPException(404, "token inválido")
        if row["revoked_at"]:
            raise HTTPException(401, "token revocado")
        if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc):
            raise HTTPException(401, "token expirado")
        # Touch
        await conn.execute(
            """
            update client_portal_tokens set last_used_at = now(), use_count = use_count + 1
             where id = $1::uuid
            """,
            row["id"],
        )
    return {
        "firm_id": str(row["firm_id"]),
        "client_id": str(row["client_id"]),
        "scope": list(row["scope"] or []),
    }


@router_public.get("/{token}")
async def portal_home(token: str):
    ctx = await _resolve_token(token)
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        client = await conn.fetchrow(
            "select id, nombre, tax_id, personal_id, email, telefono from clients where id = $1::uuid",
            ctx["client_id"],
        )
        firm = await conn.fetchrow(
            "select razon_social, nit from firms where id = $1::uuid",
            ctx["firm_id"],
        )
        matters_count = await conn.fetchval(
            "select count(*) from matters where client_id = $1::uuid and firm_id = $2::uuid",
            ctx["client_id"], ctx["firm_id"],
        )
        invoices_open = await conn.fetchval(
            """
            select count(*) from invoices
             where client_id = $1::uuid and firm_id = $2::uuid
               and status in ('sent','partially_paid','overdue')
            """,
            ctx["client_id"], ctx["firm_id"],
        )
        invoices_total_due = await conn.fetchval(
            """
            select coalesce(sum(total_cop - paid_amount_cop), 0) from invoices
             where client_id = $1::uuid and firm_id = $2::uuid
               and status in ('sent','partially_paid','overdue')
            """,
            ctx["client_id"], ctx["firm_id"],
        )
    return {
        "scope": ctx["scope"],
        "client": {
            "nombre": client["nombre"] if client else None,
            "tax_id": client["tax_id"] if client else None,
            "personal_id": client["personal_id"] if client else None,
            "email": client["email"] if client else None,
        },
        "firm": {
            "razon_social": firm["razon_social"] if firm else None,
            "nit": firm["nit"] if firm else None,
        },
        "summary": {
            "matters_count": matters_count or 0,
            "invoices_open": invoices_open or 0,
            "invoices_total_due_cop": float(invoices_total_due or 0),
        },
    }


@router_public.get("/{token}/matters")
async def portal_matters(token: str):
    ctx = await _resolve_token(token)
    if "matters" not in ctx["scope"]:
        raise HTTPException(403, "scope sin acceso a matters")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, titulo, materia, status, etapa_procesal, juzgado,
                   expediente, proxima_fecha, proxima_tipo, created_at
              from matters
             where client_id = $1::uuid and firm_id = $2::uuid
             order by created_at desc
            """,
            ctx["client_id"], ctx["firm_id"],
        )
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]),
                "titulo": r["titulo"],
                "materia": r["materia"],
                "status": r["status"],
                "etapa_procesal": r["etapa_procesal"],
                "juzgado": r["juzgado"],
                "expediente": r["expediente"],
                "proxima_fecha": r["proxima_fecha"].isoformat() if r["proxima_fecha"] else None,
                "proxima_tipo": r["proxima_tipo"],
            }
            for r in rows
        ],
    }


@router_public.get("/{token}/matters/{matter_id}")
async def portal_matter_detail(token: str, matter_id: str):
    ctx = await _resolve_token(token)
    if "matters" not in ctx["scope"]:
        raise HTTPException(403, "scope sin acceso")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        m = await conn.fetchrow(
            """
            select id, titulo, materia, status, etapa_procesal, juzgado,
                   expediente, proxima_fecha, proxima_tipo, created_at
              from matters
             where id = $1::uuid and client_id = $2::uuid and firm_id = $3::uuid
            """,
            matter_id, ctx["client_id"], ctx["firm_id"],
        )
        if not m:
            raise HTTPException(404, "matter no encontrado")
        timeline = await conn.fetch(
            """
            select titulo, descripcion, fecha, tipo
              from matter_timeline
             where matter_id = $1::uuid
             order by fecha desc limit 30
            """,
            matter_id,
        )
        deadlines = await conn.fetch(
            """
            select titulo, fecha, tipo, completado
              from matter_deadlines
             where matter_id = $1::uuid
             order by fecha asc limit 20
            """,
            matter_id,
        )
    return {
        "matter": {
            "id": str(m["id"]),
            "titulo": m["titulo"],
            "materia": m["materia"],
            "status": m["status"],
            "etapa_procesal": m["etapa_procesal"],
            "juzgado": m["juzgado"],
            "expediente": m["expediente"],
            "proxima_fecha": m["proxima_fecha"].isoformat() if m["proxima_fecha"] else None,
            "proxima_tipo": m["proxima_tipo"],
        },
        "timeline": [
            {"titulo": t["titulo"], "descripcion": t["descripcion"],
             "fecha": t["fecha"].isoformat() if t["fecha"] else None,
             "tipo": t["tipo"]}
            for t in timeline
        ],
        "deadlines": [
            {"titulo": d["titulo"],
             "fecha": d["fecha"].isoformat() if d["fecha"] else None,
             "tipo": d["tipo"], "completado": d["completado"]}
            for d in deadlines
        ],
    }


@router_public.get("/{token}/invoices")
async def portal_invoices(token: str):
    ctx = await _resolve_token(token)
    if "invoices" not in ctx["scope"]:
        raise HTTPException(403, "scope sin acceso")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, number, period_start, period_end, subtotal_cop,
                   tax_cop, total_cop, status, due_date, paid_amount_cop, sent_at
              from invoices
             where client_id = $1::uuid and firm_id = $2::uuid
               and status in ('sent','paid','partially_paid','overdue')
             order by created_at desc
            """,
            ctx["client_id"], ctx["firm_id"],
        )
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]),
                "number": r["number"],
                "period_start": r["period_start"].isoformat() if r["period_start"] else None,
                "period_end": r["period_end"].isoformat() if r["period_end"] else None,
                "subtotal_cop": float(r["subtotal_cop"]),
                "tax_cop": float(r["tax_cop"]),
                "total_cop": float(r["total_cop"]),
                "status": r["status"],
                "due_date": r["due_date"].isoformat() if r["due_date"] else None,
                "paid_amount_cop": float(r["paid_amount_cop"]),
                "sent_at": r["sent_at"].isoformat() if r["sent_at"] else None,
            }
            for r in rows
        ],
    }


@router_public.get("/{token}/invoices/{invoice_id}")
async def portal_invoice_detail(token: str, invoice_id: str):
    ctx = await _resolve_token(token)
    if "invoices" not in ctx["scope"]:
        raise HTTPException(403, "scope sin acceso")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        inv = await conn.fetchrow(
            """
            select id, number, period_start, period_end, subtotal_cop,
                   tax_pct, tax_cop, retencion_cop, total_cop, currency,
                   status, due_date, sent_at, paid_at, paid_amount_cop, notes
              from invoices
             where id = $1::uuid and client_id = $2::uuid and firm_id = $3::uuid
            """,
            invoice_id, ctx["client_id"], ctx["firm_id"],
        )
        if not inv:
            raise HTTPException(404, "factura no encontrada")
        lines = await conn.fetch(
            """
            select kind, description, qty, unit_price_cop, total_cop, position
              from invoice_lines
             where invoice_id = $1::uuid
             order by position asc
            """,
            invoice_id,
        )
    return {
        "invoice": {
            "id": str(inv["id"]),
            "number": inv["number"],
            "period_start": inv["period_start"].isoformat() if inv["period_start"] else None,
            "period_end": inv["period_end"].isoformat() if inv["period_end"] else None,
            "subtotal_cop": float(inv["subtotal_cop"]),
            "tax_pct": float(inv["tax_pct"]),
            "tax_cop": float(inv["tax_cop"]),
            "retencion_cop": float(inv["retencion_cop"]),
            "total_cop": float(inv["total_cop"]),
            "currency": inv["currency"],
            "status": inv["status"],
            "due_date": inv["due_date"].isoformat() if inv["due_date"] else None,
            "sent_at": inv["sent_at"].isoformat() if inv["sent_at"] else None,
            "paid_at": inv["paid_at"].isoformat() if inv["paid_at"] else None,
            "paid_amount_cop": float(inv["paid_amount_cop"]),
            "notes": inv["notes"],
        },
        "lines": [
            {
                "kind": l["kind"],
                "description": l["description"],
                "qty": float(l["qty"]),
                "unit_price_cop": float(l["unit_price_cop"]),
                "total_cop": float(l["total_cop"]),
                "position": l["position"],
            }
            for l in lines
        ],
    }
