"""Sprint 9 · Leads API · pipeline de prospectos.

  GET    /v1/leads                       · lista filtrable
  GET    /v1/leads/kanban                · agrupado por stage (frontend kanban)
  GET    /v1/leads/kpis?days=30          · RPC lexai_pipeline_kpis
  POST   /v1/leads                       · crear
  GET    /v1/leads/{id}                  · detalle + activities
  PATCH  /v1/leads/{id}                  · editar
  POST   /v1/leads/{id}/move-stage       · cambiar etapa (registra activity)
  POST   /v1/leads/{id}/activities       · log de actividad
  POST   /v1/leads/{id}/convert          · convertir a cliente + matter
  DELETE /v1/leads/{id}
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/leads", tags=["leads"])


def _serialize(r) -> dict:
    return {
        "id": str(r["id"]),
        "stage_id": str(r["stage_id"]) if r["stage_id"] else None,
        "nombre": r["nombre"],
        "email": r["email"],
        "telefono": r["telefono"],
        "source": r["source"],
        "materia": r["materia"],
        "estimated_value_cop": float(r["estimated_value_cop"]) if r["estimated_value_cop"] else None,
        "notes": r["notes"],
        "score": r["score"],
        "status": r["status"],
        "assigned_to": str(r["assigned_to"]) if r["assigned_to"] else None,
        "last_contact_at": r["last_contact_at"].isoformat() if r["last_contact_at"] else None,
        "next_followup_at": r["next_followup_at"].isoformat() if r["next_followup_at"] else None,
        "converted_client_id": str(r["converted_client_id"]) if r["converted_client_id"] else None,
        "converted_matter_id": str(r["converted_matter_id"]) if r["converted_matter_id"] else None,
        "converted_at": r["converted_at"].isoformat() if r["converted_at"] else None,
        "lost_reason": r["lost_reason"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


@router.get("")
async def list_leads(
    status: Optional[str] = Query(default=None, regex="^(open|won|lost|dormant)$"),
    stage_id: Optional[str] = None,
    assigned_to: Optional[str] = None,
    limit: int = Query(default=200, le=500),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    where = ["firm_id = $1::uuid"]
    params: list = [principal.firm_id]
    if status:
        params.append(status); where.append(f"status = ${len(params)}")
    if stage_id:
        params.append(stage_id); where.append(f"stage_id = ${len(params)}::uuid")
    if assigned_to:
        params.append(assigned_to); where.append(f"assigned_to = ${len(params)}::uuid")
    params.append(limit)
    sql = f"""
        select id, stage_id, nombre, email, telefono, source, materia,
               estimated_value_cop, notes, score, status, assigned_to,
               last_contact_at, next_followup_at,
               converted_client_id, converted_matter_id, converted_at, lost_reason, created_at
          from leads
         where {' and '.join(where)}
         order by created_at desc
         limit ${len(params)}
    """
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {"count": len(rows), "items": [_serialize(r) for r in rows]}


@router.get("/kanban")
async def kanban(principal: Principal = Depends(get_current_firm)):
    """Devuelve stages + leads agrupados, listo para el board."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        stages = await conn.fetch(
            """
            select id, name, sort_order, color, is_won, is_lost
              from lead_stages where firm_id = $1::uuid
             order by sort_order asc
            """,
            principal.firm_id,
        )
        leads = await conn.fetch(
            """
            select id, stage_id, nombre, email, telefono, source, materia,
                   estimated_value_cop, notes, score, status, assigned_to,
                   last_contact_at, next_followup_at,
                   converted_client_id, converted_matter_id, converted_at, lost_reason, created_at
              from leads
             where firm_id = $1::uuid and status = 'open'
             order by created_at desc
             limit 500
            """,
            principal.firm_id,
        )
    by_stage: dict[str, list] = {}
    for l in leads:
        key = str(l["stage_id"]) if l["stage_id"] else "_unassigned"
        by_stage.setdefault(key, []).append(_serialize(l))
    return {
        "stages": [
            {
                "id": str(s["id"]),
                "name": s["name"],
                "sort_order": s["sort_order"],
                "color": s["color"],
                "is_won": s["is_won"],
                "is_lost": s["is_lost"],
                "leads": by_stage.get(str(s["id"]), []),
                "count": len(by_stage.get(str(s["id"]), [])),
                "value_cop": sum(l["estimated_value_cop"] or 0 for l in by_stage.get(str(s["id"]), [])),
            }
            for s in stages
        ],
        "unassigned": by_stage.get("_unassigned", []),
    }


@router.get("/kpis")
async def kpis(
    days: int = Query(default=30, le=365),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        result = await conn.fetchval(
            "select lexai_pipeline_kpis($1::uuid, $2::int)", principal.firm_id, days,
        )
    return result or {}


class CreateRequest(BaseModel):
    nombre: str = Field(min_length=2)
    email: Optional[str] = None
    telefono: Optional[str] = None
    source: Optional[str] = None
    materia: Optional[str] = None
    estimated_value_cop: Optional[float] = None
    notes: Optional[str] = None
    stage_id: Optional[str] = None
    assigned_to: Optional[str] = None
    next_followup_at: Optional[str] = None


@router.post("")
async def create_lead(
    body: CreateRequest,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        # Si no especifica stage, usa el primero
        stage_id = body.stage_id
        if not stage_id:
            stage_id = await conn.fetchval(
                "select id from lead_stages where firm_id = $1::uuid order by sort_order asc limit 1",
                principal.firm_id,
            )
        row = await conn.fetchrow(
            """
            insert into leads
              (firm_id, stage_id, nombre, email, telefono, source, materia,
               estimated_value_cop, notes, assigned_to, next_followup_at, created_by)
            values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7,
                    $8, $9, $10::uuid, $11::timestamptz, $12::uuid)
            returning id, stage_id, nombre, email, telefono, source, materia,
                      estimated_value_cop, notes, score, status, assigned_to,
                      last_contact_at, next_followup_at,
                      converted_client_id, converted_matter_id, converted_at, lost_reason, created_at
            """,
            principal.firm_id, stage_id, body.nombre, body.email, body.telefono,
            body.source, body.materia, body.estimated_value_cop, body.notes,
            body.assigned_to, body.next_followup_at, principal.user_id,
        )
    return _serialize(row)


@router.get("/{lead_id}")
async def get_lead(
    lead_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, stage_id, nombre, email, telefono, source, materia,
                   estimated_value_cop, notes, score, status, assigned_to,
                   last_contact_at, next_followup_at,
                   converted_client_id, converted_matter_id, converted_at, lost_reason, created_at
              from leads where id = $1::uuid and firm_id = $2::uuid
            """,
            lead_id, principal.firm_id,
        )
        if not row:
            raise HTTPException(404, "not found")
        acts = await conn.fetch(
            """
            select id, kind, body, metadata, user_id, occurred_at
              from lead_activities where lead_id = $1::uuid
             order by occurred_at desc limit 100
            """,
            lead_id,
        )
    return {
        "lead": _serialize(row),
        "activities": [
            {
                "id": str(a["id"]), "kind": a["kind"], "body": a["body"],
                "metadata": a["metadata"], "user_id": str(a["user_id"]) if a["user_id"] else None,
                "occurred_at": a["occurred_at"].isoformat() if a["occurred_at"] else None,
            }
            for a in acts
        ],
    }


class PatchRequest(BaseModel):
    nombre: Optional[str] = None
    email: Optional[str] = None
    telefono: Optional[str] = None
    source: Optional[str] = None
    materia: Optional[str] = None
    estimated_value_cop: Optional[float] = None
    notes: Optional[str] = None
    score: Optional[int] = Field(default=None, ge=0, le=100)
    assigned_to: Optional[str] = None
    next_followup_at: Optional[str] = None
    status: Optional[str] = Field(default=None, pattern="^(open|won|lost|dormant)$")
    lost_reason: Optional[str] = None


@router.patch("/{lead_id}")
async def patch_lead(
    lead_id: str,
    body: PatchRequest,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    fields, params = [], [lead_id, principal.firm_id]
    for f in ("nombre", "email", "telefono", "source", "materia",
              "estimated_value_cop", "notes", "score", "assigned_to",
              "next_followup_at", "status", "lost_reason"):
        v = getattr(body, f)
        if v is not None:
            params.append(v); fields.append(f"{f} = ${len(params)}")
    if not fields:
        raise HTTPException(400, "nada que actualizar")
    sql = f"""
        update leads set {', '.join(fields)}, updated_at = now()
         where id = $1::uuid and firm_id = $2::uuid
         returning id, stage_id, nombre, email, telefono, source, materia,
                   estimated_value_cop, notes, score, status, assigned_to,
                   last_contact_at, next_followup_at,
                   converted_client_id, converted_matter_id, converted_at, lost_reason, created_at
    """
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(sql, *params)
    if not row:
        raise HTTPException(404, "not found")
    return _serialize(row)


class MoveStageRequest(BaseModel):
    stage_id: str
    note: Optional[str] = None


@router.post("/{lead_id}/move-stage")
async def move_stage(
    lead_id: str,
    body: MoveStageRequest,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        stage = await conn.fetchrow(
            "select name, is_won, is_lost from lead_stages where id = $1::uuid and firm_id = $2::uuid",
            body.stage_id, principal.firm_id,
        )
        if not stage:
            raise HTTPException(404, "stage not found")
        status = "won" if stage["is_won"] else "lost" if stage["is_lost"] else "open"
        row = await conn.fetchrow(
            """
            update leads set stage_id = $3::uuid, status = $4, updated_at = now()
             where id = $1::uuid and firm_id = $2::uuid
            returning id, stage_id, status
            """,
            lead_id, principal.firm_id, body.stage_id, status,
        )
        if not row:
            raise HTTPException(404, "lead not found")
        await conn.execute(
            """
            insert into lead_activities (firm_id, lead_id, user_id, kind, body)
            values ($1::uuid, $2::uuid, $3::uuid, 'stage_change', $4)
            """,
            principal.firm_id, lead_id, principal.user_id,
            f"→ {stage['name']}" + (f": {body.note}" if body.note else ""),
        )
    return {"id": str(row["id"]), "stage_id": str(row["stage_id"]), "status": row["status"]}


class ActivityRequest(BaseModel):
    kind: str = Field(pattern="^(note|call|email|meeting|whatsapp|stage_change)$")
    body: Optional[str] = None
    metadata: Optional[dict] = None


@router.post("/{lead_id}/activities")
async def add_activity(
    lead_id: str,
    body: ActivityRequest,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into lead_activities (firm_id, lead_id, user_id, kind, body, metadata)
            values ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6::jsonb)
            returning id, occurred_at
            """,
            principal.firm_id, lead_id, principal.user_id,
            body.kind, body.body, json.dumps(body.metadata or {}),
        )
        await conn.execute(
            "update leads set last_contact_at = now(), updated_at = now() where id = $1::uuid",
            lead_id,
        )
    return {"id": str(row["id"]), "occurred_at": row["occurred_at"].isoformat()}


class ConvertRequest(BaseModel):
    create_matter: bool = True
    materia: Optional[str] = None
    titulo_matter: Optional[str] = None


@router.post("/{lead_id}/convert")
async def convert_lead(
    lead_id: str,
    body: ConvertRequest,
    principal: Principal = Depends(get_current_firm),
):
    """Crea cliente + opcional matter, marca lead as won."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        lead = await conn.fetchrow(
            "select * from leads where id = $1::uuid and firm_id = $2::uuid",
            lead_id, principal.firm_id,
        )
        if not lead:
            raise HTTPException(404, "lead not found")
        if lead["converted_client_id"]:
            raise HTTPException(409, "ya convertido")
        # Crear cliente
        client = await conn.fetchrow(
            """
            insert into clients (firm_id, tipo, nombre, email, telefono, created_by)
            values ($1::uuid, 'natural', $2, $3, $4, $5::uuid)
            returning id
            """,
            principal.firm_id, lead["nombre"], lead["email"], lead["telefono"], principal.user_id,
        )
        client_id = client["id"]
        # Crear matter si pidió
        matter_id = None
        if body.create_matter:
            materia = body.materia or lead["materia"] or "civil"
            titulo = body.titulo_matter or f"Caso de {lead['nombre']}"
            try:
                matter = await conn.fetchrow(
                    """
                    insert into matters (firm_id, client_id, display_id, titulo, materia, status, owner_user_id)
                    values ($1::uuid, $2::uuid,
                            'M-' || to_char(now(), 'YYYYMMDD') || '-' || substring(replace($2::text,'-',''), 1, 6),
                            $3, $4::materia_legal, 'activo', $5::uuid)
                    returning id
                    """,
                    principal.firm_id, client_id, titulo, materia, principal.user_id,
                )
                matter_id = matter["id"]
            except Exception as e:
                logger.warning("create matter from lead failed: %s", e)
        # Marcar won
        won_stage = await conn.fetchval(
            "select id from lead_stages where firm_id = $1::uuid and is_won = true limit 1",
            principal.firm_id,
        )
        await conn.execute(
            """
            update leads set status = 'won', converted_client_id = $2::uuid,
                             converted_matter_id = $3::uuid, converted_at = now(),
                             stage_id = coalesce($4::uuid, stage_id), updated_at = now()
             where id = $1::uuid
            """,
            lead_id, client_id, matter_id, won_stage,
        )
        await conn.execute(
            """
            insert into lead_activities (firm_id, lead_id, user_id, kind, body)
            values ($1::uuid, $2::uuid, $3::uuid, 'note',
                    'Convertido en cliente · client_id=' || $4::text)
            """,
            principal.firm_id, lead_id, principal.user_id, str(client_id),
        )
    return {
        "lead_id": lead_id,
        "client_id": str(client_id),
        "matter_id": str(matter_id) if matter_id else None,
        "status": "won",
    }


@router.delete("/{lead_id}")
async def delete_lead(
    lead_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "delete from leads where id = $1::uuid and firm_id = $2::uuid",
            lead_id, principal.firm_id,
        )
    return {"deleted": True}


# ════════════════════════════════════════════════════════════════════════
# Voice tool
# ════════════════════════════════════════════════════════════════════════


async def capture_lead_tool(args: dict, ctx: dict) -> dict:
    """Voice: 'LexAI, agrega un nuevo prospecto Juan Pérez, teléfono 300-123-4567,
                tema laboral, viene de WhatsApp'."""
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    nombre = (args.get("nombre") or "").strip()
    if len(nombre) < 2:
        return {"error": "nombre requerido"}
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}
    async with storage.pool.acquire() as conn:
        stage_id = await conn.fetchval(
            "select id from lead_stages where firm_id = $1::uuid order by sort_order asc limit 1",
            firm_id,
        )
        row = await conn.fetchrow(
            """
            insert into leads (firm_id, stage_id, nombre, email, telefono, source, materia, notes, created_by)
            values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9::uuid)
            returning id, nombre
            """,
            firm_id, stage_id, nombre, args.get("email"), args.get("telefono"),
            args.get("source") or "voice", args.get("materia"), args.get("notes"),
            ctx.get("user_id"),
        )
    return {"id": str(row["id"]), "nombre": row["nombre"]}
