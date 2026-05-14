"""Sprint 14 · Public API · autenticada con API key (no Supabase JWT).

Pensada para integraciones externas (ERP, CRM, Zapier, Make, scripts).

  GET  /v1/public/me                · información de la API key
  GET  /v1/public/matters           · matters del firm (read scope)
  GET  /v1/public/matters/{id}
  GET  /v1/public/clients
  GET  /v1/public/clients/{id}
  GET  /v1/public/leads
  POST /v1/public/leads             · crear lead (write scope)
  GET  /v1/public/insights          · sugerencias del agente

Compatibility: solo endpoints **idempotentes y simples**. Las operaciones
complejas (Canvas, voz, firma digital) requieren el flujo completo de la
app y no se exponen aquí.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.api_keys import scope_allows
from utils.api_key_auth import ApiKeyPrincipal, require_api_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/public", tags=["public_api"])


def _scope_or_403(p: ApiKeyPrincipal, scope: str):
    if not scope_allows(p.scopes, scope):
        raise HTTPException(403, f"Scope insuficiente: requiere {scope}")


@router.get("/me")
async def whoami(principal: ApiKeyPrincipal = Depends(require_api_key)):
    return {
        "firm_id": principal.firm_id,
        "scopes": principal.scopes,
        "rate_limit_per_min": principal.rate_limit_per_min,
    }


# ──────────────────────────────────────────────────────────────────────
# Matters
# ──────────────────────────────────────────────────────────────────────


@router.get("/matters")
async def list_matters(
    status: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    principal: ApiKeyPrincipal = Depends(require_api_key),
):
    _scope_or_403(principal, "matters.read")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    where = ["firm_id = $1::uuid"]
    params: list = [principal.firm_id]
    if status:
        params.append(status); where.append(f"status = ${len(params)}")
    params.append(limit); params.append(offset)
    sql = f"""
        select id, client_id, display_id, titulo, materia::text, etapa_procesal,
               juzgado, expediente, status::text, priority::text,
               proxima_fecha, cuantia, created_at
          from matters
         where {' and '.join(where)}
         order by created_at desc
         limit ${len(params)-1} offset ${len(params)}
    """
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]), "client_id": str(r["client_id"]),
                "display_id": r["display_id"], "titulo": r["titulo"],
                "materia": r["materia"], "etapa_procesal": r["etapa_procesal"],
                "juzgado": r["juzgado"], "expediente": r["expediente"],
                "status": r["status"], "priority": r["priority"],
                "proxima_fecha": r["proxima_fecha"].isoformat() if r["proxima_fecha"] else None,
                "cuantia": float(r["cuantia"]) if r["cuantia"] is not None else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


@router.get("/matters/{matter_id}")
async def get_matter(
    matter_id: str,
    principal: ApiKeyPrincipal = Depends(require_api_key),
):
    _scope_or_403(principal, "matters.read")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, client_id, display_id, titulo, materia::text, etapa_procesal,
                   juzgado, expediente, status::text, priority::text,
                   proxima_fecha, proxima_tipo, cuantia, created_at, updated_at
              from matters
             where id = $1::uuid and firm_id = $2::uuid
            """,
            matter_id, principal.firm_id,
        )
    if not row:
        raise HTTPException(404, "not found")
    return {
        "id": str(row["id"]), "client_id": str(row["client_id"]),
        "display_id": row["display_id"], "titulo": row["titulo"],
        "materia": row["materia"], "etapa_procesal": row["etapa_procesal"],
        "juzgado": row["juzgado"], "expediente": row["expediente"],
        "status": row["status"], "priority": row["priority"],
        "proxima_fecha": row["proxima_fecha"].isoformat() if row["proxima_fecha"] else None,
        "proxima_tipo": row["proxima_tipo"],
        "cuantia": float(row["cuantia"]) if row["cuantia"] is not None else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


# ──────────────────────────────────────────────────────────────────────
# Clients
# ──────────────────────────────────────────────────────────────────────


@router.get("/clients")
async def list_clients(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    principal: ApiKeyPrincipal = Depends(require_api_key),
):
    _scope_or_403(principal, "clients.read")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, tipo, nombre, tax_id, personal_id, email, telefono, created_at
              from clients where firm_id = $1::uuid
             order by created_at desc limit $2 offset $3
            """,
            principal.firm_id, limit, offset,
        )
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]), "tipo": r["tipo"], "nombre": r["nombre"],
                "tax_id": r["tax_id"], "personal_id": r["personal_id"],
                "email": r["email"], "telefono": r["telefono"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


@router.get("/clients/{client_id}")
async def get_client(
    client_id: str,
    principal: ApiKeyPrincipal = Depends(require_api_key),
):
    _scope_or_403(principal, "clients.read")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            select id, tipo, nombre, tax_id, personal_id, email, telefono, domicilio,
                   metadata, created_at, updated_at
              from clients where id = $1::uuid and firm_id = $2::uuid
            """,
            client_id, principal.firm_id,
        )
    if not r:
        raise HTTPException(404, "not found")
    return {
        "id": str(r["id"]), "tipo": r["tipo"], "nombre": r["nombre"],
        "tax_id": r["tax_id"], "personal_id": r["personal_id"],
        "email": r["email"], "telefono": r["telefono"], "domicilio": r["domicilio"],
        "metadata": r["metadata"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


# ──────────────────────────────────────────────────────────────────────
# Leads
# ──────────────────────────────────────────────────────────────────────


@router.get("/leads")
async def list_leads(
    status: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    principal: ApiKeyPrincipal = Depends(require_api_key),
):
    _scope_or_403(principal, "leads.read")
    from utils.db import get_storage
    storage = await get_storage()
    where = ["firm_id = $1::uuid"]
    params: list = [principal.firm_id]
    if status:
        params.append(status); where.append(f"status = ${len(params)}")
    params.append(limit)
    sql = f"""
        select id, nombre, email, telefono, source, materia,
               estimated_value_cop, status, created_at
          from leads where {' and '.join(where)}
         order by created_at desc limit ${len(params)}
    """
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]), "nombre": r["nombre"], "email": r["email"],
                "telefono": r["telefono"], "source": r["source"],
                "materia": r["materia"],
                "estimated_value_cop": float(r["estimated_value_cop"]) if r["estimated_value_cop"] else None,
                "status": r["status"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


class CreateLeadRequest(BaseModel):
    nombre: str = Field(min_length=2)
    email: Optional[str] = None
    telefono: Optional[str] = None
    source: Optional[str] = "api"
    materia: Optional[str] = None
    estimated_value_cop: Optional[float] = None
    notes: Optional[str] = None


@router.post("/leads")
async def create_lead(
    body: CreateLeadRequest,
    principal: ApiKeyPrincipal = Depends(require_api_key),
):
    _scope_or_403(principal, "leads.write")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        stage_id = await conn.fetchval(
            "select id from lead_stages where firm_id = $1::uuid order by sort_order limit 1",
            principal.firm_id,
        )
        row = await conn.fetchrow(
            """
            insert into leads
              (firm_id, stage_id, nombre, email, telefono, source, materia,
               estimated_value_cop, notes)
            values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9)
            returning id, nombre, source, status, created_at
            """,
            principal.firm_id, stage_id, body.nombre, body.email, body.telefono,
            body.source, body.materia, body.estimated_value_cop, body.notes,
        )

    # Emit event para webhooks salientes
    try:
        from utils.event_emitter import emit_event
        await emit_event(
            principal.firm_id, "lead.created",
            {"id": str(row["id"]), "nombre": row["nombre"], "source": row["source"]},
        )
    except Exception as e:
        logger.debug("event_emit skipped: %s", e)

    return {
        "id": str(row["id"]),
        "nombre": row["nombre"],
        "status": row["status"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


# ──────────────────────────────────────────────────────────────────────
# Insights
# ──────────────────────────────────────────────────────────────────────


@router.get("/insights")
async def list_insights(
    status: str = Query(default="new"),
    limit: int = Query(default=20, le=100),
    principal: ApiKeyPrincipal = Depends(require_api_key),
):
    _scope_or_403(principal, "insights.read")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, kind, severity, target_type, target_id, title, body,
                   suggested_action, confidence, status, created_at
              from ai_insights
             where firm_id = $1::uuid and status = $2
             order by case severity when 'critical' then 1 when 'warning' then 2 else 3 end,
                      created_at desc
             limit $3
            """,
            principal.firm_id, status, limit,
        )
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]), "kind": r["kind"], "severity": r["severity"],
                "target_type": r["target_type"],
                "target_id": str(r["target_id"]) if r["target_id"] else None,
                "title": r["title"], "body": r["body"],
                "suggested_action": r["suggested_action"],
                "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
                "status": r["status"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


@router.get("/stats")
async def stats(principal: ApiKeyPrincipal = Depends(require_api_key)):
    """Plataforma stats para la firma owner de esta key."""
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        kpis = await conn.fetchval(
            "select lexai_firm_kpis($1::uuid, 30)", principal.firm_id,
        )
    return kpis or {}
