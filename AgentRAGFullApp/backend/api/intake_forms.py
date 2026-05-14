"""Sprint 19 · Intake forms admin API.

  GET    /v1/intake-forms                     · lista
  GET    /v1/intake-forms/stats               · counts por firm
  GET    /v1/intake-forms/{id}
  POST   /v1/intake-forms                     · crear (admin/socio)
  PATCH  /v1/intake-forms/{id}
  POST   /v1/intake-forms/{id}/activate
  POST   /v1/intake-forms/{id}/pause
  DELETE /v1/intake-forms/{id}

  GET    /v1/intake-forms/{id}/submissions    · review submissions
  POST   /v1/intake-submissions/{id}/convert-to-lead
  POST   /v1/intake-submissions/{id}/dismiss
  DELETE /v1/intake-submissions/{id}
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/intake-forms", tags=["intake_forms"])
subm_router = APIRouter(prefix="/v1/intake-submissions", tags=["intake_submissions"])


SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9\-]{0,60}[a-z0-9])?$")


def _slugify(name: str) -> str:
    s = (name or "").lower().strip()
    s = re.sub(r"[^a-z0-9\s\-]", "", s)
    s = re.sub(r"\s+", "-", s).strip("-")
    return s[:60] or "form"


class FormIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    slug: Optional[str] = None
    description: Optional[str] = None
    fields: list = Field(default_factory=list)
    thank_you_message: Optional[str] = None
    redirect_url: Optional[str] = None
    default_assignee_user_id: Optional[str] = None
    default_materia: Optional[str] = None
    brand_color: Optional[str] = "blue"
    show_firm_logo: bool = True
    honeypot_field: Optional[str] = "website"


class FormPatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    fields: Optional[list] = None
    thank_you_message: Optional[str] = None
    redirect_url: Optional[str] = None
    default_assignee_user_id: Optional[str] = None
    default_materia: Optional[str] = None
    brand_color: Optional[str] = None
    show_firm_logo: Optional[bool] = None
    honeypot_field: Optional[str] = None


def _serialize_form(r) -> dict:
    return {
        "id": str(r["id"]),
        "slug": r["slug"],
        "name": r["name"],
        "description": r["description"],
        "fields": r["fields"] if not isinstance(r["fields"], str) else (json.loads(r["fields"]) if r["fields"] else []),
        "thank_you_message": r["thank_you_message"],
        "redirect_url": r["redirect_url"],
        "default_assignee_user_id": str(r["default_assignee_user_id"]) if r["default_assignee_user_id"] else None,
        "default_materia": r["default_materia"],
        "brand_color": r["brand_color"],
        "show_firm_logo": bool(r["show_firm_logo"]),
        "honeypot_field": r["honeypot_field"],
        "active": bool(r["active"]),
        "submissions_count": int(r["submissions_count"] or 0),
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


def _serialize_subm(r) -> dict:
    keys = set(r.keys()) if hasattr(r, "keys") else set()
    return {
        "id": str(r["id"]),
        "intake_form_id": str(r["intake_form_id"]),
        "form_name": r["form_name"] if "form_name" in keys else None,
        "payload": r["payload"] if not isinstance(r["payload"], str) else (json.loads(r["payload"]) if r["payload"] else {}),
        "submitter_email": r["submitter_email"],
        "submitter_nombre": r["submitter_nombre"],
        "submitter_phone": r["submitter_phone"],
        "status": r["status"],
        "converted_lead_id": str(r["converted_lead_id"]) if r["converted_lead_id"] else None,
        "converted_at": r["converted_at"].isoformat() if r["converted_at"] else None,
        "notes": r["notes"],
        "ip_address": str(r["ip_address"]) if r["ip_address"] else None,
        "user_agent": r["user_agent"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


def _require_admin(p: Principal):
    if p.role not in ("admin", "socio_senior", "socio_junior"):
        raise HTTPException(403, "Solo admin / socio puede gestionar intake forms")


# --------------------------------------------------------------------
# Forms CRUD
# --------------------------------------------------------------------
@router.get("/stats")
async def stats(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {}
    async with storage.pool.acquire() as conn:
        raw = await conn.fetchval("select lexai_intake_stats($1::uuid)", principal.firm_id)
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return raw


@router.get("")
async def list_forms(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, slug, name, description, fields, thank_you_message,
                   redirect_url, default_assignee_user_id, default_materia,
                   brand_color, show_firm_logo, honeypot_field,
                   active, submissions_count, created_at, updated_at
              from intake_forms
             where firm_id = $1::uuid
             order by active desc, created_at desc
            """,
            principal.firm_id,
        )
    return {"items": [_serialize_form(r) for r in rows]}


@router.get("/{form_id}")
async def get_form(
    form_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            "select * from intake_forms where firm_id = $1::uuid and id = $2::uuid",
            principal.firm_id, form_id,
        )
    if not row:
        raise HTTPException(404, "Form no encontrado")
    return _serialize_form(row)


@router.post("", status_code=201)
async def create_form(
    body: FormIn,
    principal: Principal = Depends(get_current_firm),
):
    _require_admin(principal)
    from utils.intake_validators import validate_field_schema
    errors = validate_field_schema(body.fields)
    if errors:
        raise HTTPException(400, {"detail": "Schema inválido", "errors": errors})

    slug = (body.slug or _slugify(body.name)).strip().lower()
    if not SLUG_RE.match(slug):
        raise HTTPException(400, "Slug inválido (lowercase + dígitos + guión, max 60)")

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                insert into intake_forms
                  (firm_id, slug, name, description, fields, thank_you_message,
                   redirect_url, default_assignee_user_id, default_materia,
                   brand_color, show_firm_logo, honeypot_field, created_by)
                values ($1::uuid, $2, $3, $4, $5::jsonb, $6, $7, $8::uuid, $9,
                        $10, $11, $12, $13::uuid)
                returning *
                """,
                principal.firm_id, slug, body.name, body.description,
                json.dumps(body.fields or []),
                body.thank_you_message or 'Gracias. Te contactaremos pronto.',
                body.redirect_url, body.default_assignee_user_id,
                body.default_materia, body.brand_color or 'blue',
                body.show_firm_logo, body.honeypot_field or 'website',
                principal.user_id,
            )
        except Exception as e:
            if "unique" in str(e).lower():
                raise HTTPException(409, "Slug ya en uso para este despacho")
            raise HTTPException(400, f"No se pudo crear: {e}")
    return _serialize_form(row)


@router.patch("/{form_id}")
async def update_form(
    form_id: str,
    body: FormPatch,
    principal: Principal = Depends(get_current_firm),
):
    _require_admin(principal)
    from utils.intake_validators import validate_field_schema

    sets: list[str] = []
    args: list = []
    idx = 1
    if body.fields is not None:
        errors = validate_field_schema(body.fields)
        if errors:
            raise HTTPException(400, {"detail": "Schema inválido", "errors": errors})
        sets.append(f"fields = ${idx}::jsonb")
        args.append(json.dumps(body.fields)); idx += 1
    for col, val in (
        ("name", body.name), ("description", body.description),
        ("thank_you_message", body.thank_you_message),
        ("redirect_url", body.redirect_url),
        ("default_materia", body.default_materia),
        ("brand_color", body.brand_color),
        ("honeypot_field", body.honeypot_field),
    ):
        if val is not None:
            sets.append(f"{col} = ${idx}")
            args.append(val); idx += 1
    if body.default_assignee_user_id is not None:
        sets.append(f"default_assignee_user_id = ${idx}::uuid")
        args.append(body.default_assignee_user_id or None); idx += 1
    if body.show_firm_logo is not None:
        sets.append(f"show_firm_logo = ${idx}")
        args.append(body.show_firm_logo); idx += 1
    if not sets:
        raise HTTPException(400, "Sin cambios")
    args.append(principal.firm_id)
    args.append(form_id)

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            update intake_forms set {', '.join(sets)}
             where firm_id = ${idx}::uuid and id = ${idx + 1}::uuid
             returning *
            """,
            *args,
        )
    if not row:
        raise HTTPException(404, "Form no encontrado")
    return _serialize_form(row)


@router.post("/{form_id}/activate")
async def activate_form(form_id: str, principal: Principal = Depends(get_current_firm)):
    _require_admin(principal)
    return await _set_active(principal.firm_id, form_id, True)


@router.post("/{form_id}/pause")
async def pause_form(form_id: str, principal: Principal = Depends(get_current_firm)):
    _require_admin(principal)
    return await _set_active(principal.firm_id, form_id, False)


async def _set_active(firm_id: str, form_id: str, active: bool) -> dict:
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update intake_forms set active = $1
             where firm_id = $2::uuid and id = $3::uuid
             returning id, active
            """,
            active, firm_id, form_id,
        )
    if not row:
        raise HTTPException(404, "Form no encontrado")
    return {"id": str(row["id"]), "active": row["active"]}


@router.delete("/{form_id}")
async def delete_form(form_id: str, principal: Principal = Depends(get_current_firm)):
    _require_admin(principal)
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "delete from intake_forms where firm_id = $1::uuid and id = $2::uuid",
            principal.firm_id, form_id,
        )
    return {"deleted": True}


# --------------------------------------------------------------------
# Submissions list
# --------------------------------------------------------------------
@router.get("/{form_id}/submissions")
async def list_submissions(
    form_id: str,
    status: Optional[str] = Query(default=None, pattern="^(new|converted|spam|dismissed)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_current_firm),
):
    where = ["firm_id = $1::uuid", "intake_form_id = $2::uuid"]
    args: list = [principal.firm_id, form_id]
    idx = 3
    if status:
        where.append(f"status = ${idx}"); args.append(status); idx += 1
    args.extend([limit, offset])
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            select s.*, f.name as form_name
              from intake_submissions s
              join intake_forms f on f.id = s.intake_form_id
             where {' and '.join(where)}
             order by s.created_at desc
             limit ${idx} offset ${idx + 1}
            """,
            *args,
        )
    return {"items": [_serialize_subm(r) for r in rows]}


# --------------------------------------------------------------------
# Submissions actions (separate /v1/intake-submissions router)
# --------------------------------------------------------------------
@subm_router.get("/{subm_id}")
async def get_submission(
    subm_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select s.*, f.name as form_name
              from intake_submissions s
              join intake_forms f on f.id = s.intake_form_id
             where s.firm_id = $1::uuid and s.id = $2::uuid
            """,
            principal.firm_id, subm_id,
        )
    if not row:
        raise HTTPException(404, "Submission no encontrada")
    return _serialize_subm(row)


@subm_router.post("/{subm_id}/convert-to-lead")
async def convert_to_lead(
    subm_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        subm = await conn.fetchrow(
            """
            select s.*, f.default_assignee_user_id, f.default_materia
              from intake_submissions s
              join intake_forms f on f.id = s.intake_form_id
             where s.firm_id = $1::uuid and s.id = $2::uuid
            """,
            principal.firm_id, subm_id,
        )
        if not subm:
            raise HTTPException(404, "Submission no encontrada")
        if subm["status"] == "converted":
            raise HTTPException(409, "Esta submission ya fue convertida")
        payload = subm["payload"] if not isinstance(subm["payload"], str) else (
            json.loads(subm["payload"]) if subm["payload"] else {}
        )

        # Heurísticas para poblar el lead
        nombre = subm["submitter_nombre"] or _guess(payload, ["nombre", "name", "full_name"]) or "Cliente intake"
        email = subm["submitter_email"] or _guess(payload, ["email"])
        telefono = subm["submitter_phone"] or _guess(payload, ["telefono", "phone", "celular"])
        materia = subm["default_materia"] or _guess(payload, ["materia", "tipo_caso"])
        notes_parts = []
        for k, v in (payload or {}).items():
            if k in ("nombre", "email", "telefono", "phone", "materia"):
                continue
            if v is None or v == "":
                continue
            notes_parts.append(f"{k}: {v}")
        notes = "\n".join(notes_parts)[:4000]
        assigned_to = subm["default_assignee_user_id"]

        lead_row = await conn.fetchrow(
            """
            insert into leads
              (firm_id, nombre, email, telefono, source, materia, notes,
               assigned_to, metadata, created_by)
            values ($1::uuid, $2, $3, $4, 'web', $5, $6, $7::uuid, $8::jsonb, $9::uuid)
            returning id
            """,
            principal.firm_id, nombre, email, telefono, materia, notes,
            assigned_to, json.dumps({"intake_submission_id": str(subm_id)}),
            principal.user_id,
        )
        await conn.execute(
            """
            update intake_submissions
               set status = 'converted', converted_lead_id = $1::uuid, converted_at = now()
             where firm_id = $2::uuid and id = $3::uuid
            """,
            lead_row["id"], principal.firm_id, subm_id,
        )
    return {"ok": True, "lead_id": str(lead_row["id"]), "submission_id": subm_id}


def _guess(payload: dict, keys: list[str]) -> Optional[str]:
    if not payload:
        return None
    for k in keys:
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


@subm_router.post("/{subm_id}/dismiss")
async def dismiss_submission(
    subm_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            update intake_submissions set status = 'dismissed'
             where firm_id = $1::uuid and id = $2::uuid
            """,
            principal.firm_id, subm_id,
        )
    return {"dismissed": True}


@subm_router.delete("/{subm_id}")
async def delete_submission(
    subm_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "delete from intake_submissions where firm_id = $1::uuid and id = $2::uuid",
            principal.firm_id, subm_id,
        )
    return {"deleted": True}
