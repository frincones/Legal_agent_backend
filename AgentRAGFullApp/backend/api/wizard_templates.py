"""Sprint 22 · Wizard templates admin API.

  GET  /v1/wizard-templates           · list system + own custom
  GET  /v1/wizard-templates/{id}
  POST /v1/wizard-templates           · custom · per firm
  PATCH /v1/wizard-templates/{id}     · custom only · per firm
  DELETE /v1/wizard-templates/{id}    · custom only · per firm
  GET  /v1/wizard-templates/stats     · stats por firm
  GET  /v1/wizard-sessions            · sessions del firm (analytics)
  GET  /v1/wizard-sessions/{id}
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
router = APIRouter(prefix="/v1/wizard-templates", tags=["wizard_templates"])
sessions_router = APIRouter(prefix="/v1/wizard-sessions", tags=["wizard_sessions"])


VALID_CATEGORIES = {"pension", "derecho_peticion", "tutela", "contrato", "denuncia", "otro"}
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9\-]{0,60}[a-z0-9])?$")


class TemplateIn(BaseModel):
    slug: str
    name: str
    description: Optional[str] = None
    category: str
    icon: Optional[str] = None
    steps: list = Field(default_factory=list)
    document_template: str
    document_title: Optional[str] = None
    identity_validations: list = Field(default_factory=list)
    brand_color: Optional[str] = "blue"
    legal_disclaimer: Optional[str] = None
    output_actions: Optional[list[str]] = None
    defensoria_email: Optional[str] = None
    lead_assignee_user_id: Optional[str] = None


class TemplatePatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    steps: Optional[list] = None
    document_template: Optional[str] = None
    document_title: Optional[str] = None
    identity_validations: Optional[list] = None
    brand_color: Optional[str] = None
    legal_disclaimer: Optional[str] = None
    output_actions: Optional[list[str]] = None
    defensoria_email: Optional[str] = None
    lead_assignee_user_id: Optional[str] = None
    active: Optional[bool] = None


def _serialize_tpl(r) -> dict:
    return {
        "id": str(r["id"]),
        "slug": r["slug"],
        "name": r["name"],
        "description": r["description"],
        "category": r["category"],
        "icon": r["icon"],
        "steps": _parse_json(r["steps"]),
        "document_template": r["document_template"],
        "document_title": r["document_title"],
        "identity_validations": _parse_json(r["identity_validations"]),
        "output_actions": list(r["output_actions"] or []),
        "defensoria_email": r["defensoria_email"],
        "lead_assignee_user_id": str(r["lead_assignee_user_id"]) if r["lead_assignee_user_id"] else None,
        "brand_color": r["brand_color"],
        "legal_disclaimer": r["legal_disclaimer"],
        "is_system": bool(r["is_system"]),
        "active": bool(r["active"]),
        "sessions_count": int(r["sessions_count"] or 0),
        "completions_count": int(r["completions_count"] or 0),
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


def _parse_json(v):
    if v is None:
        return []
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return []
    return v


# --------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------
@router.get("/stats")
async def stats(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {}
    async with storage.pool.acquire() as conn:
        raw = await conn.fetchval(
            "select lexai_wizard_stats($1::uuid)", principal.firm_id,
        )
    return raw if not isinstance(raw, str) else json.loads(raw or "{}")


@router.get("")
async def list_templates(
    category: Optional[str] = Query(default=None),
    only_custom: bool = Query(default=False),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    where = ["active = true"]
    args: list = []
    if only_custom:
        where.append("firm_id = $1::uuid and is_system = false")
        args.append(principal.firm_id)
    else:
        where.append("(is_system = true or firm_id = $1::uuid)")
        args.append(principal.firm_id)
    idx = len(args) + 1
    if category:
        if category not in VALID_CATEGORIES:
            raise HTTPException(400, f"category inválida (válidos: {sorted(VALID_CATEGORIES)})")
        where.append(f"category = ${idx}"); args.append(category)
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            select * from wizard_templates
             where {' and '.join(where)}
             order by is_system desc, category, name
            """,
            *args,
        )
    return {"items": [_serialize_tpl(r) for r in rows]}


@router.get("/{template_id}")
async def get_template(
    template_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select * from wizard_templates
             where id = $1::uuid
               and (is_system = true or firm_id = $2::uuid)
            """,
            template_id, principal.firm_id,
        )
    if not row:
        raise HTTPException(404, "Template no encontrado")
    return _serialize_tpl(row)


def _require_admin(p: Principal):
    if p.role not in ("admin", "socio_senior", "socio_junior",
                       "in_house", "independiente", "consultor"):
        raise HTTPException(403, "Solo admin / socio puede crear wizards custom")


@router.post("", status_code=201)
async def create_template(
    body: TemplateIn,
    principal: Principal = Depends(get_current_firm),
):
    _require_admin(principal)
    slug = (body.slug or "").strip().lower()
    if not SLUG_RE.match(slug):
        raise HTTPException(400, "Slug inválido (lowercase + dígitos + guión)")
    if body.category not in VALID_CATEGORIES:
        raise HTTPException(400, f"category inválida")
    if not body.document_template:
        raise HTTPException(400, "document_template requerido")

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                insert into wizard_templates
                  (firm_id, is_system, slug, name, description, category, icon,
                   steps, document_template, document_title, identity_validations,
                   output_actions, defensoria_email, lead_assignee_user_id,
                   brand_color, legal_disclaimer, created_by)
                values ($1::uuid, false, $2, $3, $4, $5, $6,
                        $7::jsonb, $8, $9, $10::jsonb,
                        $11::text[], $12, $13::uuid, $14, $15, $16::uuid)
                returning *
                """,
                principal.firm_id, slug, body.name, body.description,
                body.category, body.icon,
                json.dumps(body.steps or []), body.document_template,
                body.document_title or body.name,
                json.dumps(body.identity_validations or []),
                body.output_actions or ['download_docx', 'download_pdf'],
                body.defensoria_email, body.lead_assignee_user_id,
                body.brand_color or 'blue', body.legal_disclaimer,
                principal.user_id,
            )
        except Exception as e:
            msg = str(e).lower()
            if "unique" in msg or "duplicate" in msg:
                raise HTTPException(409, "Slug ya en uso por este firm")
            raise HTTPException(400, f"No se pudo crear: {e}")
    return _serialize_tpl(row)


@router.patch("/{template_id}")
async def update_template(
    template_id: str,
    body: TemplatePatch,
    principal: Principal = Depends(get_current_firm),
):
    _require_admin(principal)
    sets: list[str] = []
    args: list = []
    idx = 1
    for col, val in (
        ("name", body.name), ("description", body.description), ("icon", body.icon),
        ("document_template", body.document_template),
        ("document_title", body.document_title), ("brand_color", body.brand_color),
        ("legal_disclaimer", body.legal_disclaimer),
        ("defensoria_email", body.defensoria_email),
    ):
        if val is not None:
            sets.append(f"{col} = ${idx}"); args.append(val); idx += 1
    if body.steps is not None:
        sets.append(f"steps = ${idx}::jsonb"); args.append(json.dumps(body.steps)); idx += 1
    if body.identity_validations is not None:
        sets.append(f"identity_validations = ${idx}::jsonb")
        args.append(json.dumps(body.identity_validations)); idx += 1
    if body.output_actions is not None:
        sets.append(f"output_actions = ${idx}::text[]")
        args.append(body.output_actions); idx += 1
    if body.lead_assignee_user_id is not None:
        sets.append(f"lead_assignee_user_id = ${idx}::uuid")
        args.append(body.lead_assignee_user_id or None); idx += 1
    if body.active is not None:
        sets.append(f"active = ${idx}"); args.append(body.active); idx += 1
    if not sets:
        raise HTTPException(400, "Sin cambios")
    args.append(principal.firm_id)
    args.append(template_id)
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            update wizard_templates set {', '.join(sets)}
             where firm_id = ${idx}::uuid and id = ${idx + 1}::uuid
               and is_system = false
             returning *
            """,
            *args,
        )
    if not row:
        raise HTTPException(404, "Template no encontrado o es system (no editable)")
    return _serialize_tpl(row)


@router.delete("/{template_id}")
async def delete_template(
    template_id: str,
    principal: Principal = Depends(get_current_firm),
):
    _require_admin(principal)
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "delete from wizard_templates where firm_id = $1::uuid and id = $2::uuid and is_system = false",
            principal.firm_id, template_id,
        )
    return {"deleted": True}


# --------------------------------------------------------------------
# Sessions (admin viewer)
# --------------------------------------------------------------------
def _serialize_session(r) -> dict:
    keys = set(r.keys()) if hasattr(r, "keys") else set()
    def _opt(k):
        return r[k] if k in keys else None
    return {
        "id": str(r["id"]),
        "wizard_template_id": str(r["wizard_template_id"]),
        "template_name": _opt("template_name"),
        "template_slug": _opt("template_slug"),
        "session_token": r["session_token"],
        "current_step": int(r["current_step"] or 0),
        "completed_steps": list(r["completed_steps"] or []),
        "status": r["status"],
        "submitted_action": r["submitted_action"],
        "submitter_email": r["submitter_email"],
        "submitter_name": r["submitter_name"],
        "submitter_phone": r["submitter_phone"],
        "routed_to_firm_id": str(r["routed_to_firm_id"]) if r["routed_to_firm_id"] else None,
        "routed_to_lead_id": str(r["routed_to_lead_id"]) if r["routed_to_lead_id"] else None,
        "routed_to_email": r["routed_to_email"],
        "document_title": _opt("document_title"),
        "ip_address": str(r["ip_address"]) if r["ip_address"] else None,
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
        "answers_summary": _summarize_answers(_parse_json(r["answers"]) if "answers" in keys else None),
    }


def _summarize_answers(answers):
    if not isinstance(answers, dict):
        return {}
    out = {}
    for k in ("nombre", "cedula", "email", "telefono", "asunto"):
        if k in answers and answers[k]:
            out[k] = str(answers[k])[:120]
    return out


@sessions_router.get("")
async def list_sessions(
    status: Optional[str] = Query(default=None, pattern="^(in_progress|completed|submitted|abandoned|error)$"),
    template_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    where = ["s.routed_to_firm_id = $1::uuid"]
    args: list = [principal.firm_id]
    idx = 2
    if status:
        where.append(f"s.status = ${idx}"); args.append(status); idx += 1
    if template_id:
        where.append(f"s.wizard_template_id = ${idx}::uuid"); args.append(template_id); idx += 1
    args.append(limit)
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            select s.*, t.name as template_name, t.slug as template_slug
              from wizard_sessions s
              join wizard_templates t on t.id = s.wizard_template_id
             where {' and '.join(where)}
             order by s.created_at desc
             limit ${idx}
            """,
            *args,
        )
    return {"items": [_serialize_session(r) for r in rows]}


@sessions_router.get("/{session_id}")
async def get_session(
    session_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select s.*, t.name as template_name, t.slug as template_slug,
                   t.document_template, t.document_title as tpl_title
              from wizard_sessions s
              join wizard_templates t on t.id = s.wizard_template_id
             where s.id = $1::uuid
               and (s.routed_to_firm_id = $2::uuid or auth.role() = 'service_role')
            """,
            session_id, principal.firm_id,
        )
    if not row:
        raise HTTPException(404, "Session no encontrada")
    data = _serialize_session(row)
    keys = set(row.keys())
    data["answers"] = _parse_json(row["answers"]) if "answers" in keys else {}
    data["generated_doc_text"] = row["generated_doc_text"] if "generated_doc_text" in keys else None
    return data
