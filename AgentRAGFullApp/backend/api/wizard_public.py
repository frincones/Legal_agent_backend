"""Sprint 22 · Wizard public API · SIN AUTH.

Endpoints públicos para que ciudadanos sin cuenta puedan correr wizards.

  GET  /v1/public/wizards                            · lista wizards system
  GET  /v1/public/wizards/{slug}                     · trae template (sin firm_id)
  POST /v1/public/wizards/{slug}/sessions            · crea session anónima
  GET  /v1/public/wizards/sessions/{token}           · resume session por token
  PATCH /v1/public/wizards/sessions/{token}/answers  · guarda answers parciales
  POST /v1/public/wizards/sessions/{token}/advance   · avanza un step (con validación)
  POST /v1/public/wizards/sessions/{token}/generate  · render del documento
  POST /v1/public/wizards/sessions/{token}/submit    · acción final (download / email)

Rate-limit por IP · honeypot opcional.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/public/wizards", tags=["wizards_public"])


# Rate limit in-memory (mismo patrón que intake_public.py)
_RL: dict[str, deque] = {}
_RL_WINDOW = 3600


def _check_rate_limit(ip: str, limit: int = 30) -> bool:
    now = time.time()
    bucket = _RL.setdefault(ip, deque())
    cutoff = now - _RL_WINDOW
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


def _parse_json(v):
    if v is None:
        return []
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return []
    return v


def _client_ip(request: Request) -> str:
    return (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )


# --------------------------------------------------------------------
# GET list of system wizards
# --------------------------------------------------------------------
@router.get("")
async def list_public_wizards():
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch("select * from lexai_wizard_system_list()")
    return {
        "items": [
            {
                "id": str(r["id"]),
                "slug": r["slug"],
                "name": r["name"],
                "description": r["description"],
                "category": r["category"],
                "icon": r["icon"],
                "brand_color": r["brand_color"],
            }
            for r in rows
        ]
    }


# --------------------------------------------------------------------
# GET single template by slug
# --------------------------------------------------------------------
@router.get("/{slug}")
async def get_public_template(slug: str):
    slug = (slug or "").strip().lower()
    if not slug or len(slug) > 80:
        raise HTTPException(404, "Wizard no encontrado")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            "select * from lexai_wizard_template_by_slug($1)", slug,
        )
        firm_name = None
        if row and row["firm_id"]:
            f = await conn.fetchrow("select razon_social from firms where id = $1::uuid", row["firm_id"])
            if f:
                firm_name = f["razon_social"]
    if not row:
        raise HTTPException(404, "Wizard no encontrado o inactivo")
    return {
        "id": str(row["id"]),
        "slug": row["slug"],
        "name": row["name"],
        "description": row["description"],
        "category": row["category"],
        "icon": row["icon"],
        "steps": _parse_json(row["steps"]),
        "document_title": row["document_title"],
        "output_actions": list(row["output_actions"] or []) if "output_actions" in row.keys() else ['download_docx'],
        "brand_color": row["brand_color"],
        "legal_disclaimer": row["legal_disclaimer"],
        "is_system": bool(row["is_system"]),
        "firm_name": firm_name,
    }


# --------------------------------------------------------------------
# Sessions · create + resume + advance + generate + submit
# --------------------------------------------------------------------
@router.post("/{slug}/sessions", status_code=201)
async def create_session(slug: str, request: Request):
    slug = (slug or "").strip().lower()
    ip = _client_ip(request)
    if not _check_rate_limit(ip):
        raise HTTPException(429, "Demasiados intentos · intenta más tarde")
    from utils.db import get_storage
    from utils.wizard_helpers import random_token
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        tpl = await conn.fetchrow(
            "select * from lexai_wizard_template_by_slug($1)", slug,
        )
        if not tpl:
            raise HTTPException(404, "Wizard no encontrado o inactivo")
        token = random_token()
        ua = request.headers.get("user-agent", "")[:500]
        firm_id = tpl["firm_id"]
        row = await conn.fetchrow(
            """
            insert into wizard_sessions
              (wizard_template_id, session_token, ip_address, user_agent,
               routed_to_firm_id, document_title)
            values ($1::uuid, $2, $3::inet, $4, $5::uuid, $6)
            returning id, session_token, created_at
            """,
            tpl["id"], token, ip if ip != "unknown" else None, ua,
            firm_id, tpl["document_title"],
        )
    return {
        "id": str(row["id"]),
        "session_token": row["session_token"],
        "wizard_slug": slug,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


@router.get("/sessions/{token}")
async def get_session_by_token(token: str):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select s.*, t.slug as template_slug, t.name as template_name,
                   t.steps as template_steps, t.brand_color, t.icon,
                   t.legal_disclaimer, t.output_actions, t.identity_validations
              from wizard_sessions s
              join wizard_templates t on t.id = s.wizard_template_id
             where s.session_token = $1
            """,
            token,
        )
    if not row:
        raise HTTPException(404, "Session no encontrada")
    return {
        "id": str(row["id"]),
        "session_token": row["session_token"],
        "wizard_template_id": str(row["wizard_template_id"]),
        "template_slug": row["template_slug"],
        "template_name": row["template_name"],
        "template_steps": _parse_json(row["template_steps"]),
        "template_brand_color": row["brand_color"],
        "template_icon": row["icon"],
        "template_legal_disclaimer": row["legal_disclaimer"],
        "template_output_actions": list(row["output_actions"] or []),
        "template_identity_validations": _parse_json(row["identity_validations"]),
        "answers": _parse_json(row["answers"]) or {},
        "current_step": int(row["current_step"] or 0),
        "completed_steps": list(row["completed_steps"] or []),
        "status": row["status"],
        "generated_doc_text": row["generated_doc_text"],
        "document_title": row["document_title"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


class AnswersIn:
    pass


@router.patch("/sessions/{token}/answers")
async def update_answers(token: str, request: Request):
    """Patch parcial de answers · no avanza step."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON inválido")
    new_answers = body.get("answers")
    if not isinstance(new_answers, dict):
        raise HTTPException(400, "answers debe ser objeto")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        sess = await conn.fetchrow(
            "select id, answers, status from wizard_sessions where session_token = $1",
            token,
        )
        if not sess:
            raise HTTPException(404, "Session no encontrada")
        if sess["status"] in ("submitted", "abandoned"):
            raise HTTPException(409, f"Session ya en estado {sess['status']}")
        existing = _parse_json(sess["answers"]) or {}
        if not isinstance(existing, dict):
            existing = {}
        merged = {**existing, **new_answers}
        row = await conn.fetchrow(
            """
            update wizard_sessions
               set answers = $1::jsonb
             where session_token = $2
             returning id, answers, updated_at
            """,
            json.dumps(merged), token,
        )
    return {"ok": True, "answers": merged, "updated_at": row["updated_at"].isoformat()}


@router.post("/sessions/{token}/advance")
async def advance_step(token: str, request: Request):
    """Avanza un step · valida answers contra el schema del step actual."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    new_answers = body.get("answers") or {}
    if not isinstance(new_answers, dict):
        raise HTTPException(400, "answers debe ser objeto")

    from utils.db import get_storage
    from utils.wizard_helpers import validate_step_answers
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")

    async with storage.pool.acquire() as conn:
        sess = await conn.fetchrow(
            """
            select s.*, t.steps as template_steps
              from wizard_sessions s
              join wizard_templates t on t.id = s.wizard_template_id
             where s.session_token = $1
            """,
            token,
        )
        if not sess:
            raise HTTPException(404, "Session no encontrada")
        if sess["status"] in ("submitted", "abandoned"):
            raise HTTPException(409, f"Session ya en estado {sess['status']}")

        steps = _parse_json(sess["template_steps"]) or []
        current_idx = int(sess["current_step"] or 0)
        if current_idx >= len(steps):
            raise HTTPException(400, "Ya completaste todos los pasos")
        current_step = steps[current_idx]

        existing = _parse_json(sess["answers"]) or {}
        if not isinstance(existing, dict):
            existing = {}
        merged = {**existing, **new_answers}
        errors = validate_step_answers(current_step, merged)
        if errors:
            raise HTTPException(400, {"detail": "Errores de validación", "errors": errors})

        completed = list(sess["completed_steps"] or [])
        step_id = current_step.get("id") or f"step_{current_idx}"
        if step_id not in completed:
            completed.append(step_id)
        next_idx = current_idx + 1
        is_last = next_idx >= len(steps)
        new_status = "completed" if is_last else "in_progress"

        await conn.execute(
            """
            update wizard_sessions
               set answers = $1::jsonb,
                   current_step = $2,
                   completed_steps = $3::text[],
                   status = $4,
                   completed_at = case when $4 = 'completed' then now() else completed_at end
             where session_token = $5
            """,
            json.dumps(merged), next_idx, completed, new_status, token,
        )

    return {
        "ok": True,
        "current_step": next_idx,
        "is_last": is_last,
        "status": new_status,
    }


@router.post("/sessions/{token}/generate")
async def generate_document(token: str):
    """Render del documento final usando el template + answers."""
    from utils.db import get_storage
    from utils.wizard_helpers import render_template
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")

    async with storage.pool.acquire() as conn:
        sess = await conn.fetchrow(
            """
            select s.*, t.document_template, t.document_title as tpl_title
              from wizard_sessions s
              join wizard_templates t on t.id = s.wizard_template_id
             where s.session_token = $1
            """,
            token,
        )
        if not sess:
            raise HTTPException(404, "Session no encontrada")

        answers = _parse_json(sess["answers"]) or {}
        rendered = render_template(sess["document_template"], answers)

        await conn.execute(
            """
            update wizard_sessions
               set generated_doc_text = $1
             where session_token = $2
            """,
            rendered, token,
        )

    return {
        "ok": True,
        "document_title": sess["document_title"] or sess["tpl_title"],
        "document_text": rendered,
    }


@router.post("/sessions/{token}/submit")
async def submit_session(token: str, request: Request):
    """Ejecuta la acción final · 'downloaded' | 'emailed_defensoria' | 'lead_created'."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    action = (body.get("action") or "downloaded").strip().lower()
    submitter_email = (body.get("submitter_email") or "").strip() or None
    submitter_name = (body.get("submitter_name") or "").strip() or None
    submitter_phone = (body.get("submitter_phone") or "").strip() or None

    if action not in ("downloaded", "emailed_defensoria", "lead_created"):
        raise HTTPException(400, "action inválida")

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")

    async with storage.pool.acquire() as conn:
        sess = await conn.fetchrow(
            """
            select s.*, t.defensoria_email, t.lead_assignee_user_id,
                   t.name as template_name
              from wizard_sessions s
              join wizard_templates t on t.id = s.wizard_template_id
             where s.session_token = $1
            """,
            token,
        )
        if not sess:
            raise HTTPException(404, "Session no encontrada")
        if sess["status"] not in ("completed", "in_progress"):
            raise HTTPException(409, f"Session ya está {sess['status']}")
        answers = _parse_json(sess["answers"]) or {}

        # Inferir email/nombre del payload si no se pasan
        if not submitter_email:
            submitter_email = answers.get("email")
        if not submitter_name:
            submitter_name = answers.get("nombre")
        if not submitter_phone:
            submitter_phone = answers.get("telefono")

        routed_to_email = None
        routed_to_lead_id = None

        if action == "emailed_defensoria":
            routed_to_email = sess["defensoria_email"] or "atencion@defensoria.gov.co"
            # En producción: enviar email via Resend/SendGrid · placeholder.
            logger.info("emailed_defensoria · session=%s · email=%s", sess["id"], routed_to_email)

        elif action == "lead_created":
            # Si la session tiene routed_to_firm_id (wizard custom de un firm),
            # creamos un lead automáticamente.
            firm_id = sess["routed_to_firm_id"]
            if firm_id:
                try:
                    notes = "\n".join(f"{k}: {v}" for k, v in answers.items() if v)[:4000]
                    lead_row = await conn.fetchrow(
                        """
                        insert into leads
                          (firm_id, nombre, email, telefono, source, materia, notes,
                           assigned_to, metadata)
                        values ($1::uuid, $2, $3, $4, 'wizard', $5, $6, $7::uuid, $8::jsonb)
                        returning id
                        """,
                        firm_id, submitter_name or "Wizard B2C",
                        submitter_email, submitter_phone,
                        sess["template_name"],
                        notes, sess["lead_assignee_user_id"],
                        json.dumps({"wizard_session_id": str(sess["id"])}),
                    )
                    routed_to_lead_id = lead_row["id"]
                except Exception as e:
                    logger.warning("lead_created failed: %s", e)

        await conn.execute(
            """
            update wizard_sessions
               set status = 'submitted',
                   submitted_action = $1,
                   submitter_email = $2,
                   submitter_name = $3,
                   submitter_phone = $4,
                   routed_to_email = $5,
                   routed_to_lead_id = $6::uuid,
                   completed_at = coalesce(completed_at, now())
             where session_token = $7
            """,
            action, submitter_email, submitter_name, submitter_phone,
            routed_to_email, routed_to_lead_id, token,
        )

    return {
        "ok": True,
        "action": action,
        "routed_to_email": routed_to_email,
        "lead_id": str(routed_to_lead_id) if routed_to_lead_id else None,
    }
