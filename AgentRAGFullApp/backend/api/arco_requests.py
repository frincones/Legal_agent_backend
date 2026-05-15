"""Sprint 28 · ARCO Habeas Data · Ley 1581/2012 (CO) + LFPDPPP (MX).

Endpoints cliente (firm-scoped):
  POST  /v1/me/arco-requests              · crear solicitud
  GET   /v1/me/arco-requests              · listar mis solicitudes
  GET   /v1/me/arco-requests/{id}         · detalle + mensajes
  POST  /v1/me/arco-requests/{id}/messages · responder con info adicional

Endpoint público (sin auth · para no-clientes):
  POST  /v1/public/arco-requests          · crear solicitud externa

Endpoints admin:
  GET   /v1/admin/arco-requests           · queue completo
  GET   /v1/admin/arco-requests/{id}      · detalle
  POST  /v1/admin/arco-requests/{id}/assign · asignar a admin
  POST  /v1/admin/arco-requests/{id}/status · cambiar status
  POST  /v1/admin/arco-requests/{id}/messages · responder cliente
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm
from utils.admin_guard import (
    AdminPrincipal, require_saas_admin, audit_admin_action,
)
from utils.rate_limiter import rate_limit_dep

logger = logging.getLogger(__name__)
client_router = APIRouter(prefix="/v1/me/arco-requests", tags=["arco-client"])
public_router = APIRouter(prefix="/v1/public/arco-requests", tags=["arco-public"])
admin_router = APIRouter(prefix="/v1/admin/arco-requests", tags=["arco-admin"])


VALID_KINDS = ('access', 'rectification', 'cancellation', 'opposition', 'portability', 'consent_revocation')


# ══════════════════════════════════════════════════════════════════════
# CLIENT (firm-scoped · authenticated)
# ══════════════════════════════════════════════════════════════════════


class ArcoCreate(BaseModel):
    request_kind: str = Field(pattern="^(access|rectification|cancellation|opposition|portability|consent_revocation)$")
    subject: str = Field(min_length=5, max_length=200)
    description: str = Field(min_length=20, max_length=5000)
    data_subject_id: Optional[str] = None
    evidence_url: Optional[str] = None


@client_router.post("")
async def client_create_arco(
    body: ArcoCreate, request: Request,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into arco_requests
              (firm_id, user_id, request_kind, requestor_email, requestor_name,
               subject, description, data_subject_id, evidence_url, source_law,
               metadata)
            values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9, 'Ley 1581/2012 (CO)',
                    jsonb_build_object('source', 'app_authenticated'))
            returning id, created_at, due_at
            """,
            principal.firm_id, principal.user_id, body.request_kind,
            principal.email or 'unknown@firm',
            (principal.raw_claims or {}).get('name') or principal.email,
            body.subject, body.description, body.data_subject_id, body.evidence_url,
        )
    return {"ok": True, "id": str(row["id"]), "due_at": row["due_at"]}


@client_router.get("")
async def client_list_arco(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, request_kind, status, priority, subject, due_at,
                   created_at, updated_at, response_at, closed_at
              from arco_requests
             where firm_id = $1::uuid
             order by created_at desc
            """,
            principal.firm_id,
        )
    return {"items": [dict(r) for r in rows]}


@client_router.get("/{request_id}")
async def client_arco_detail(
    request_id: str, principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        req = await conn.fetchrow(
            "select * from arco_requests where id = $1::uuid and firm_id = $2::uuid",
            request_id, principal.firm_id,
        )
        if not req:
            raise HTTPException(404, "request not found")
        msgs = await conn.fetch(
            """
            select id, author_kind, body, created_at, admin_user_id, user_id
              from arco_request_messages
             where request_id = $1::uuid and not internal_note
             order by created_at asc
            """, request_id,
        )
    return {"request": dict(req), "messages": [dict(m) for m in msgs]}


class ArcoMessage(BaseModel):
    body: str = Field(min_length=2, max_length=5000)


@client_router.post("/{request_id}/messages")
async def client_arco_reply(
    request_id: str, body: ArcoMessage,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        owner = await conn.fetchval(
            "select firm_id from arco_requests where id = $1::uuid", request_id,
        )
        if str(owner) != principal.firm_id:
            raise HTTPException(404, "request not found")
        await conn.execute(
            """
            insert into arco_request_messages (request_id, author_kind, user_id, body)
            values ($1::uuid, 'requestor', $2::uuid, $3)
            """,
            request_id, principal.user_id, body.body,
        )
        # Re-abre el ticket si estaba waiting_user
        await conn.execute(
            "update arco_requests set status = case when status = 'requires_info' then 'in_progress' else status end, "
            "updated_at = now() where id = $1::uuid",
            request_id,
        )
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════
# PUBLIC (no auth · para no-clientes que ejercen derecho ARCO)
# Con rate limit · 3 solicitudes por hora por IP
# ══════════════════════════════════════════════════════════════════════


class ArcoPublicCreate(BaseModel):
    request_kind: str = Field(pattern="^(access|rectification|cancellation|opposition|portability|consent_revocation)$")
    requestor_email: str = Field(min_length=5, max_length=200,
                                  pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    requestor_name: str = Field(min_length=2, max_length=200)
    requestor_doc_id: Optional[str] = None
    subject: str = Field(min_length=5, max_length=200)
    description: str = Field(min_length=20, max_length=5000)
    data_subject_id: Optional[str] = None


@public_router.post("",
    dependencies=[Depends(rate_limit_dep(limit=3, window_seconds=3600, scope="ip"))],
)
async def public_create_arco(body: ArcoPublicCreate, request: Request):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into arco_requests
              (request_kind, requestor_email, requestor_name, requestor_doc_id,
               subject, description, data_subject_id, source_law, metadata)
            values ($1, $2, $3, $4, $5, $6, $7, 'Ley 1581/2012 (CO)',
                    jsonb_build_object('source', 'public_form',
                                       'ip', $8::text,
                                       'user_agent', $9))
            returning id, created_at, due_at
            """,
            body.request_kind, body.requestor_email.lower(), body.requestor_name,
            body.requestor_doc_id, body.subject, body.description, body.data_subject_id,
            request.client.host if request.client else None,
            request.headers.get("user-agent", "")[:200],
        )
    return {
        "ok": True,
        "request_id": str(row["id"]),
        "message": "Tu solicitud fue recibida. Recibirás respuesta en máximo 15 días hábiles según Ley 1581/2012.",
        "due_at": row["due_at"],
    }


# ══════════════════════════════════════════════════════════════════════
# ADMIN
# ══════════════════════════════════════════════════════════════════════


@admin_router.get("")
async def admin_list_arco(
    status_filter: Optional[str] = None,
    limit: int = 50,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    where = ["1=1"]
    params: list = []
    if status_filter:
        params.append(status_filter); where.append(f"r.status = ${len(params)}")
    params.append(limit)
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            select r.*, f.razon_social as firm_name, au.email as assigned_email,
                   case when r.due_at < now() and r.status not in ('completed','cancelled','rejected')
                        then true else false end as overdue,
                   extract(day from (r.due_at - now())) as days_to_due
              from arco_requests r
              left join firms f on f.id = r.firm_id
              left join admin_users au on au.id = r.assigned_to
             where {' and '.join(where)}
             order by
               case r.priority when 'urgent' then 0 when 'high' then 1 when 'normal' then 2 else 3 end,
               r.due_at asc
             limit ${len(params)}
            """,
            *params,
        )
        total = await conn.fetchval("select count(*) from arco_requests")
    return {"items": [dict(r) for r in rows], "total": total or 0}


@admin_router.get("/{request_id}")
async def admin_arco_detail(
    request_id: str, admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        req = await conn.fetchrow(
            """
            select r.*, f.razon_social as firm_name, au.email as assigned_email
              from arco_requests r
              left join firms f on f.id = r.firm_id
              left join admin_users au on au.id = r.assigned_to
             where r.id = $1::uuid
            """, request_id,
        )
        if not req:
            raise HTTPException(404, "request not found")
        msgs = await conn.fetch(
            """
            select m.*, au.email as admin_email
              from arco_request_messages m
              left join admin_users au on au.id = m.admin_user_id
             where m.request_id = $1::uuid
             order by m.created_at asc
            """, request_id,
        )
    return {"request": dict(req), "messages": [dict(m) for m in msgs]}


class AssignBody(BaseModel):
    admin_user_id: Optional[str] = None


@admin_router.post("/{request_id}/assign")
async def admin_arco_assign(
    request_id: str, body: AssignBody, request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "update arco_requests set assigned_to = $2::uuid, updated_at = now() where id = $1::uuid",
            request_id, body.admin_user_id,
        )
    await audit_admin_action(admin, "arco.assign", resource_type="arco_request",
                             resource_id=request_id, request=request,
                             metadata={"assigned_to": body.admin_user_id})
    return {"ok": True}


class StatusBody(BaseModel):
    status: str = Field(pattern="^(open|in_progress|requires_info|approved|rejected|completed|cancelled)$")
    response_text: Optional[str] = None


@admin_router.post("/{request_id}/status")
async def admin_arco_status(
    request_id: str, body: StatusBody, request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            update arco_requests set
              status = $2,
              response_text = coalesce($3, response_text),
              response_at = case when $3 is not null then now() else response_at end,
              closed_at = case when $2 in ('completed','cancelled','rejected') then now() else closed_at end,
              updated_at = now()
             where id = $1::uuid
            """,
            request_id, body.status, body.response_text,
        )
    await audit_admin_action(admin, "arco.status_change", resource_type="arco_request",
                             resource_id=request_id, request=request,
                             metadata={"status": body.status})
    return {"ok": True}


class AdminMessage(BaseModel):
    body: str = Field(min_length=2, max_length=5000)
    internal_note: bool = False


@admin_router.post("/{request_id}/messages")
async def admin_arco_reply(
    request_id: str, body: AdminMessage, request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            insert into arco_request_messages
              (request_id, author_kind, admin_user_id, body, internal_note)
            values ($1::uuid, 'admin', $2::uuid, $3, $4)
            """,
            request_id, admin.admin_user_id, body.body, body.internal_note,
        )
        if not body.internal_note:
            await conn.execute(
                "update arco_requests set status = case when status = 'open' then 'in_progress' else status end, "
                "updated_at = now() where id = $1::uuid",
                request_id,
            )
    await audit_admin_action(admin, "arco.reply", resource_type="arco_request",
                             resource_id=request_id, request=request,
                             metadata={"internal_note": body.internal_note})
    return {"ok": True}
