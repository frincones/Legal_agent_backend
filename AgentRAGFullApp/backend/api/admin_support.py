"""Sprint 24 · SaaS Admin · Support tickets.

Endpoints admin:
  GET   /v1/admin/support/tickets               · queue con filtros
  GET   /v1/admin/support/tickets/{ticket_id}   · detalle + mensajes
  POST  /v1/admin/support/tickets/{ticket_id}/assign     · assign a admin
  POST  /v1/admin/support/tickets/{ticket_id}/status     · cambiar status
  POST  /v1/admin/support/tickets/{ticket_id}/messages   · responder (con flag internal_note)

Endpoint cliente (firm-scoped, no admin):
  POST  /v1/support/tickets                     · crear ticket desde la app
  GET   /v1/support/tickets/mine                · ver mis tickets
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from utils.admin_guard import (
    AdminPrincipal, require_saas_admin, audit_admin_action,
)
from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router_admin = APIRouter(prefix="/v1/admin/support", tags=["admin-support"])
router_client = APIRouter(prefix="/v1/support", tags=["support"])


# ══════════════════════════════════════════════════════════════════════
# ADMIN
# ══════════════════════════════════════════════════════════════════════


@router_admin.get("/tickets")
async def list_tickets(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    category: Optional[str] = None,
    assigned_to: Optional[str] = None,
    q: Optional[str] = None,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    where = ["1=1"]
    params: list = []
    if status:
        params.append(status); where.append(f"t.status = ${len(params)}")
    if priority:
        params.append(priority); where.append(f"t.priority = ${len(params)}")
    if category:
        params.append(category); where.append(f"t.category = ${len(params)}")
    if assigned_to:
        params.append(assigned_to); where.append(f"t.assigned_to = ${len(params)}::uuid")
    if q:
        params.append(f"%{q.lower()}%")
        where.append(f"(lower(t.subject) like ${len(params)} or lower(t.body) like ${len(params)})")
    params.extend([limit, offset])
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            select t.id, t.firm_id, f.razon_social as firm_name,
                   t.reporter_email, t.subject, t.category, t.status, t.priority,
                   t.assigned_to, au.email as assigned_email,
                   t.created_at, t.updated_at, t.resolved_at,
                   (select count(*) from support_ticket_messages m where m.ticket_id = t.id) as messages_count
              from support_tickets t
              left join firms f on f.id = t.firm_id
              left join admin_users au on au.id = t.assigned_to
             where {' and '.join(where)}
             order by
               case t.priority when 'urgent' then 0 when 'high' then 1 when 'normal' then 2 else 3 end,
               t.created_at desc
             limit ${len(params) - 1} offset ${len(params)}
            """, *params,
        )
        total = await conn.fetchval("select count(*) from support_tickets")
    return {"items": [dict(r) for r in rows], "total": total or 0, "limit": limit, "offset": offset}


@router_admin.get("/tickets/{ticket_id}")
async def ticket_detail(
    ticket_id: str,
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        t = await conn.fetchrow(
            """
            select t.*, f.razon_social as firm_name,
                   au.email as assigned_email
              from support_tickets t
              left join firms f on f.id = t.firm_id
              left join admin_users au on au.id = t.assigned_to
             where t.id = $1::uuid
            """, ticket_id,
        )
        if not t:
            raise HTTPException(404, "ticket not found")
        msgs = await conn.fetch(
            """
            select m.*, au.email as admin_email, u.full_name as user_name
              from support_ticket_messages m
              left join admin_users au on au.id = m.admin_user_id
              left join users u on u.id = m.user_id
             where m.ticket_id = $1::uuid
             order by m.created_at asc
            """, ticket_id,
        )
    return {"ticket": dict(t), "messages": [dict(m) for m in msgs]}


class AssignBody(BaseModel):
    admin_user_id: Optional[str] = None  # null → unassign


@router_admin.post("/tickets/{ticket_id}/assign")
async def assign_ticket(
    ticket_id: str,
    body: AssignBody,
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "update support_tickets set assigned_to = $2::uuid, updated_at = now() where id = $1::uuid",
            ticket_id, body.admin_user_id,
        )
    await audit_admin_action(admin, "support.assign", resource_type="ticket",
                             resource_id=ticket_id, request=request,
                             metadata={"assigned_to": body.admin_user_id})
    return {"ok": True}


class StatusChange(BaseModel):
    status: str = Field(pattern="^(open|in_progress|waiting_user|resolved|closed)$")


@router_admin.post("/tickets/{ticket_id}/status")
async def change_status(
    ticket_id: str,
    body: StatusChange,
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            update support_tickets
               set status = $2,
                   resolved_at = case when $2 = 'resolved' then now() else resolved_at end,
                   closed_at = case when $2 = 'closed' then now() else closed_at end,
                   updated_at = now()
             where id = $1::uuid
            """,
            ticket_id, body.status,
        )
    await audit_admin_action(admin, "support.status_change", resource_type="ticket",
                             resource_id=ticket_id, request=request,
                             metadata={"new_status": body.status})
    return {"ok": True}


class MessageCreate(BaseModel):
    body: str = Field(min_length=1, max_length=10_000)
    internal_note: bool = False


@router_admin.post("/tickets/{ticket_id}/messages")
async def reply_ticket(
    ticket_id: str,
    body: MessageCreate,
    request: Request,
    admin: AdminPrincipal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into support_ticket_messages
              (ticket_id, author_kind, admin_user_id, body, internal_note)
            values ($1::uuid, 'admin', $2::uuid, $3, $4)
            returning id, created_at
            """,
            ticket_id, admin.admin_user_id, body.body, body.internal_note,
        )
        if not body.internal_note:
            # Mover el ticket a in_progress si está en open
            await conn.execute(
                "update support_tickets set status = case when status = 'open' then 'in_progress' else status end, "
                "updated_at = now() where id = $1::uuid",
                ticket_id,
            )
    await audit_admin_action(admin, "support.reply", resource_type="ticket",
                             resource_id=ticket_id, request=request,
                             metadata={"internal_note": body.internal_note})
    return {"ok": True, "message_id": str(row["id"]), "created_at": row["created_at"]}


# ══════════════════════════════════════════════════════════════════════
# CLIENT (firm-scoped)
# ══════════════════════════════════════════════════════════════════════


class TicketCreate(BaseModel):
    subject: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=10, max_length=10_000)
    category: str = Field(default="general", pattern="^(general|bug|billing|feature_request|account|onboarding)$")
    priority: str = Field(default="normal", pattern="^(low|normal|high|urgent)$")


@router_client.post("/tickets")
async def create_ticket(
    body: TicketCreate,
    request: Request,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into support_tickets
              (firm_id, user_id, reporter_email, subject, body, category, priority)
            values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7)
            returning id, created_at
            """,
            principal.firm_id, principal.user_id, principal.email or "unknown@firm",
            body.subject, body.body, body.category, body.priority,
        )
    return {"ok": True, "ticket_id": str(row["id"]), "created_at": row["created_at"]}


@router_client.get("/tickets/mine")
async def list_my_tickets(
    limit: int = 20,
    offset: int = 0,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, subject, category, status, priority, created_at, updated_at, resolved_at
              from support_tickets
             where firm_id = $1::uuid
             order by created_at desc
             limit $2 offset $3
            """,
            principal.firm_id, limit, offset,
        )
    return {"items": [dict(r) for r in rows]}


@router_client.get("/tickets/mine/{ticket_id}")
async def my_ticket_detail(
    ticket_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        t = await conn.fetchrow(
            "select * from support_tickets where id = $1::uuid and firm_id = $2::uuid",
            ticket_id, principal.firm_id,
        )
        if not t:
            raise HTTPException(404, "ticket not found")
        msgs = await conn.fetch(
            """
            select id, author_kind, body, created_at
              from support_ticket_messages
             where ticket_id = $1::uuid and not internal_note
             order by created_at asc
            """, ticket_id,
        )
    return {"ticket": dict(t), "messages": [dict(m) for m in msgs]}


class ClientMessageCreate(BaseModel):
    body: str = Field(min_length=1, max_length=10_000)


@router_client.post("/tickets/mine/{ticket_id}/messages")
async def reply_my_ticket(
    ticket_id: str,
    body: ClientMessageCreate,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        owner = await conn.fetchval(
            "select firm_id from support_tickets where id = $1::uuid", ticket_id,
        )
        if str(owner) != principal.firm_id:
            raise HTTPException(404, "ticket not found")
        await conn.execute(
            """
            insert into support_ticket_messages
              (ticket_id, author_kind, user_id, body)
            values ($1::uuid, 'reporter', $2::uuid, $3)
            """,
            ticket_id, principal.user_id, body.body,
        )
        await conn.execute(
            "update support_tickets set status = case when status = 'resolved' then 'open' "
            "                                     when status = 'waiting_user' then 'in_progress' "
            "                                     else status end, "
            "updated_at = now() where id = $1::uuid",
            ticket_id,
        )
    return {"ok": True}
