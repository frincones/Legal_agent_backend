"""Firm Teams API · sub-equipos del socio (Sprint 1 / TASK-S1-02).

Modela el patrón "equipito" descrito por Natalia en el discovery: cada
socio puede crear N equipos con sus asociados a cargo. Multi-tenant
estricto vía RLS + verificación de firm_id.

Endpoints:
  GET    /v1/firm-teams/             → listar equipos de la firma
  POST   /v1/firm-teams/             → crear equipo (solo socios)
  GET    /v1/firm-teams/{id}         → detalle (con miembros)
  PATCH  /v1/firm-teams/{id}         → actualizar nombre/área/desc
  DELETE /v1/firm-teams/{id}         → eliminar equipo (solo socio_id == requester)
  POST   /v1/firm-teams/{id}/members → agregar miembro
  DELETE /v1/firm-teams/{id}/members/{user_id} → remover miembro
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
router = APIRouter(prefix="/v1/firm-teams", tags=["firm_teams"])


# ────────────────────────────────────────────────────────────────────
# Schemas
# ────────────────────────────────────────────────────────────────────

class TeamCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    area_practica: Optional[str] = Field(
        None,
        description="Enum materia_legal (laboral, civil, comercial, …)",
    )
    description: Optional[str] = Field(None, max_length=500)
    metadata: dict = Field(default_factory=dict)


class TeamUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=120)
    area_practica: Optional[str] = None
    description: Optional[str] = Field(None, max_length=500)
    metadata: Optional[dict] = None


class TeamMember(BaseModel):
    user_id: str
    role_in_team: str = "asociado"
    full_name: Optional[str] = None
    email: Optional[str] = None
    added_at: Optional[datetime] = None


class TeamRow(BaseModel):
    id: str
    socio_id: str
    socio_name: Optional[str] = None
    name: str
    area_practica: Optional[str]
    description: Optional[str]
    member_count: int
    created_at: datetime


class TeamDetail(TeamRow):
    members: list[TeamMember] = Field(default_factory=list)


class AddMemberRequest(BaseModel):
    user_id: str
    role_in_team: str = Field(
        default="asociado",
        pattern="^(socio_lider|asociado|asistente|paralegal|externo)$",
    )


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────

PARTNER_ROLES = {"admin", "socio_senior", "socio_junior"}


def _ensure_partner(principal: Principal) -> None:
    if principal.role not in PARTNER_ROLES:
        raise HTTPException(
            403,
            "Solo socios y admins pueden crear o eliminar equipos. "
            f"Tu rol actual: {principal.role}.",
        )


def _row_to_team(r) -> TeamRow:
    return TeamRow(
        id=str(r["id"]),
        socio_id=str(r["socio_id"]),
        socio_name=r.get("socio_name"),
        name=r["name"],
        area_practica=r["area_practica"],
        description=r["description"],
        member_count=int(r.get("member_count") or 0),
        created_at=r["created_at"],
    )


# ────────────────────────────────────────────────────────────────────
# Endpoints
# ────────────────────────────────────────────────────────────────────

@router.get("/", response_model=list[TeamRow])
async def list_teams(
    principal: Principal = Depends(get_current_firm),
    socio_id: Optional[str] = Query(None, description="Filtra por socio."),
    limit: int = Query(default=50, le=200),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        if socio_id:
            rows = await conn.fetch(
                """
                select t.id, t.socio_id, t.name, t.area_practica, t.description,
                       t.created_at,
                       u.full_name as socio_name,
                       (select count(*) from firm_team_members m where m.team_id = t.id) as member_count
                from firm_teams t
                left join users u on u.id = t.socio_id
                where t.firm_id = $1::uuid and t.socio_id = $2::uuid
                order by t.created_at desc
                limit $3
                """,
                principal.firm_id, socio_id, limit,
            )
        else:
            rows = await conn.fetch(
                """
                select t.id, t.socio_id, t.name, t.area_practica, t.description,
                       t.created_at,
                       u.full_name as socio_name,
                       (select count(*) from firm_team_members m where m.team_id = t.id) as member_count
                from firm_teams t
                left join users u on u.id = t.socio_id
                where t.firm_id = $1::uuid
                order by t.created_at desc
                limit $2
                """,
                principal.firm_id, limit,
            )
    return [_row_to_team(dict(r)) for r in rows]


@router.post("/", response_model=TeamRow, status_code=201)
async def create_team(
    body: TeamCreate,
    principal: Principal = Depends(get_current_firm),
):
    _ensure_partner(principal)
    tid = str(uuid.uuid4())
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                insert into firm_teams
                  (id, firm_id, socio_id, name, area_practica, description, metadata)
                values
                  ($1::uuid, $2::uuid, $3::uuid, $4, $5::materia_legal, $6, $7::jsonb)
                returning id, socio_id, name, area_practica, description, created_at
                """,
                tid,
                principal.firm_id,
                principal.user_id,
                body.name,
                body.area_practica,
                body.description,
                json.dumps(body.metadata),
            )
        except Exception as e:
            logger.error("create_team failed: %s", e)
            raise HTTPException(400, f"No se pudo crear equipo: {e}")
        # Auto-add the creator as socio_lider.
        await conn.execute(
            """
            insert into firm_team_members (team_id, user_id, role_in_team)
            values ($1::uuid, $2::uuid, 'socio_lider')
            on conflict do nothing
            """,
            tid, principal.user_id,
        )
    return _row_to_team({**dict(row), "member_count": 1, "socio_name": None})


@router.get("/{team_id}", response_model=TeamDetail)
async def get_team(
    team_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select t.id, t.socio_id, t.name, t.area_practica, t.description,
                   t.created_at,
                   u.full_name as socio_name
            from firm_teams t
            left join users u on u.id = t.socio_id
            where t.id = $1::uuid and t.firm_id = $2::uuid
            """,
            team_id, principal.firm_id,
        )
        if not row:
            raise HTTPException(404, "equipo no encontrado")
        members = await conn.fetch(
            """
            select m.user_id, m.role_in_team, m.added_at,
                   u.full_name, u.email
            from firm_team_members m
            left join users u on u.id = m.user_id
            where m.team_id = $1::uuid
            order by m.added_at asc
            """,
            team_id,
        )
    base = _row_to_team({**dict(row), "member_count": len(members)})
    return TeamDetail(
        **base.model_dump(),
        members=[
            TeamMember(
                user_id=str(m["user_id"]),
                role_in_team=m["role_in_team"],
                full_name=m["full_name"],
                email=m["email"],
                added_at=m["added_at"],
            )
            for m in members
        ],
    )


@router.patch("/{team_id}", response_model=TeamRow)
async def update_team(
    team_id: str,
    body: TeamUpdate,
    principal: Principal = Depends(get_current_firm),
):
    fields, params = [], []
    if body.name is not None:
        params.append(body.name)
        fields.append(f"name = ${len(params)}")
    if body.area_practica is not None:
        params.append(body.area_practica)
        fields.append(f"area_practica = ${len(params)}::materia_legal")
    if body.description is not None:
        params.append(body.description)
        fields.append(f"description = ${len(params)}")
    if body.metadata is not None:
        params.append(json.dumps(body.metadata))
        fields.append(f"metadata = ${len(params)}::jsonb")
    if not fields:
        raise HTTPException(400, "nada que actualizar")
    fields.append(f"updated_at = now()")
    params.append(team_id)
    params.append(principal.firm_id)
    sql = f"""
        update firm_teams set {', '.join(fields)}
         where id = ${len(params) - 1}::uuid and firm_id = ${len(params)}::uuid
        returning id, socio_id, name, area_practica, description, created_at
    """
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(sql, *params)
    if not row:
        raise HTTPException(404, "equipo no encontrado")
    return _row_to_team({**dict(row), "member_count": 0, "socio_name": None})


@router.delete("/{team_id}", status_code=204)
async def delete_team(
    team_id: str,
    principal: Principal = Depends(get_current_firm),
):
    _ensure_partner(principal)
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        result = await conn.execute(
            """
            delete from firm_teams
             where id = $1::uuid
               and firm_id = $2::uuid
               and (socio_id = $3::uuid or $4 = 'admin')
            """,
            team_id, principal.firm_id, principal.user_id, principal.role,
        )
    if result.endswith(" 0"):
        raise HTTPException(404, "equipo no encontrado o sin permisos")


# ────────────────────────────────────────────────────────────────────
# Members
# ────────────────────────────────────────────────────────────────────

@router.post("/{team_id}/members", status_code=201)
async def add_member(
    team_id: str,
    body: AddMemberRequest,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        # Validate team belongs to firm
        team = await conn.fetchrow(
            "select id from firm_teams where id = $1::uuid and firm_id = $2::uuid",
            team_id, principal.firm_id,
        )
        if not team:
            raise HTTPException(404, "equipo no encontrado")
        # Validate user belongs to firm
        member = await conn.fetchrow(
            "select id from users where id = $1::uuid and firm_id = $2::uuid",
            body.user_id, principal.firm_id,
        )
        if not member:
            raise HTTPException(400, "usuario no pertenece a esta firma")
        await conn.execute(
            """
            insert into firm_team_members (team_id, user_id, role_in_team)
            values ($1::uuid, $2::uuid, $3)
            on conflict (team_id, user_id) do update
              set role_in_team = excluded.role_in_team
            """,
            team_id, body.user_id, body.role_in_team,
        )
    return {"team_id": team_id, "user_id": body.user_id, "role_in_team": body.role_in_team}


@router.delete("/{team_id}/members/{user_id}", status_code=204)
async def remove_member(
    team_id: str,
    user_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        result = await conn.execute(
            """
            delete from firm_team_members
             where team_id = $1::uuid
               and user_id = $2::uuid
               and team_id in (select id from firm_teams where firm_id = $3::uuid)
            """,
            team_id, user_id, principal.firm_id,
        )
    if result.endswith(" 0"):
        raise HTTPException(404, "miembro no encontrado")
