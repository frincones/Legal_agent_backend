"""Clients API · multi-tenant CRUD over `clients` table.

LFPDPPP fields tracked:
  · consent_lfpdppp_at + consent_lfpdppp_version + consent_finalidades[]
  · consent_voice_recording (used to enable voice features per client)
  · arco_requests JSONB log (Acceso, Rectificación, Cancelación, Oposición)
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/clients", tags=["clients"])


class ClientCreate(BaseModel):
    tipo: str = Field(pattern="^(persona_natural|persona_juridica)$")
    nombre: str = Field(min_length=2)
    tax_id: Optional[str] = None         # NIT (CO) | RFC (MX) | RUT (CL)
    personal_id: Optional[str] = None    # cédula ciudadanía / CURP / DNI
    email: Optional[str] = None
    telefono: Optional[str] = None
    domicilio: Optional[dict] = None
    vip: bool = False
    consent_lfpdppp: bool = False
    consent_finalidades: list[str] = Field(default_factory=list)
    consent_voice_recording: bool = False
    metadata: dict = Field(default_factory=dict)


class ClientRow(BaseModel):
    id: str
    tipo: str
    nombre: str
    tax_id: Optional[str]
    personal_id: Optional[str]
    email: Optional[str]
    telefono: Optional[str]
    vip: bool
    consent_lfpdppp_at: Optional[datetime]
    consent_voice_recording: bool
    created_at: datetime


def _row(r) -> ClientRow:
    return ClientRow(
        id=str(r["id"]),
        tipo=r["tipo"],
        nombre=r["nombre"],
        tax_id=r["tax_id"],
        personal_id=r["personal_id"],
        email=r["email"],
        telefono=r["telefono"],
        vip=r["vip"],
        consent_lfpdppp_at=r["consent_lfpdppp_at"],
        consent_voice_recording=r["consent_voice_recording"],
        created_at=r["created_at"],
    )


@router.get("/", response_model=list[ClientRow])
async def list_clients(
    principal: Principal = Depends(get_current_firm),
    q: Optional[str] = None,
    limit: int = Query(default=50, le=200),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        if q:
            rows = await conn.fetch(
                """
                select id, tipo, nombre, tax_id, personal_id, email, telefono, vip,
                       consent_at as consent_lfpdppp_at, consent_voice_recording, created_at
                from clients
                where firm_id = $1::uuid and nombre ilike '%' || $2 || '%'
                order by nombre asc
                limit $3
                """,
                principal.firm_id, q, limit,
            )
        else:
            rows = await conn.fetch(
                """
                select id, tipo, nombre, tax_id, personal_id, email, telefono, vip,
                       consent_at as consent_lfpdppp_at, consent_voice_recording, created_at
                from clients
                where firm_id = $1::uuid
                order by created_at desc
                limit $2
                """,
                principal.firm_id, limit,
            )
    return [_row(r) for r in rows]


@router.post("/", response_model=ClientRow, status_code=201)
async def create_client(
    body: ClientCreate,
    principal: Principal = Depends(get_current_firm),
):
    cid = str(uuid.uuid4())
    consent_at = datetime.utcnow() if body.consent_lfpdppp else None
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into clients
              (id, firm_id, tipo, nombre, tax_id, personal_id, email, telefono, domicilio,
               vip, consent_at, consent_finalidades, consent_voice_recording,
               created_by, metadata)
            values
              ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9::jsonb,
               $10, $11, $12, $13, $14::uuid, $15::jsonb)
            returning id, tipo, nombre, tax_id, personal_id, email, telefono, vip,
                      consent_at as consent_lfpdppp_at, consent_voice_recording, created_at
            """,
            cid,
            principal.firm_id,
            body.tipo,
            body.nombre,
            body.tax_id,
            body.personal_id,
            body.email,
            body.telefono,
            json.dumps(body.domicilio or {}),
            body.vip,
            consent_at,
            body.consent_finalidades,
            body.consent_voice_recording,
            principal.user_id,
            json.dumps(body.metadata),
        )
    return _row(row)


@router.get("/{client_id}", response_model=ClientRow)
async def get_client(
    client_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, tipo, nombre, tax_id, personal_id, email, telefono, vip,
                   consent_at as consent_lfpdppp_at, consent_voice_recording, created_at
            from clients
            where id = $1::uuid and firm_id = $2::uuid
            """,
            client_id, principal.firm_id,
        )
    if not row:
        raise HTTPException(404, "client not found")
    return _row(row)
