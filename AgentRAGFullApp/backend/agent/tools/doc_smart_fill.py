"""Sprint 19 · Voice/agent tools de Smart Document Fill + Intake.

  · autofill_template(template_body, matter_id?, extra_text?)
       Recibe el body de un template y devuelve dict { var: valor }
       extraído del matter context (Sprint 19 LLM extractor).

  · extract_variables_from_text(text, variables)
       LLM extrae lo que pidas.

  · list_intake_forms()
       Lista los forms activos del firm con su URL pública.

  · list_new_submissions(limit?)
       Submissions recientes sin convertir → para que el agente
       le diga al user "tienes 3 leads nuevos".

ctx esperado: firm_id, user_id, matter_id (opcional).
"""

from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def autofill_template_tool(args: dict, ctx: dict) -> dict:
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    if not matter_id:
        return {"error": "Necesito un matter_id para tomar contexto"}
    template_body = (args.get("template_body") or "").strip()
    extra_text = (args.get("extra_text") or "").strip() or None

    from utils.db import get_storage
    from utils.variable_extractor import (
        extract_variables, parse_template_variables, gather_matter_context_text,
    )
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "Storage no disponible"}

    explicit_vars = args.get("variables")
    variables: list[dict]
    if isinstance(explicit_vars, list) and explicit_vars:
        variables = explicit_vars
    elif template_body:
        names = parse_template_variables(template_body)
        variables = [{"name": n, "kind": "text"} for n in names]
    else:
        return {"error": "Dame template_body o variables explícitas"}

    if not variables:
        return {"filled": {}, "message": "El template no tiene variables {{...}}"}

    async with storage.pool.acquire() as conn:
        ctx_text = await gather_matter_context_text(conn, firm_id, matter_id)
    if not ctx_text and not extra_text:
        return {"error": "Caso no encontrado o sin contexto suficiente"}
    text = (ctx_text + ("\n\n# Adicional\n" + extra_text if extra_text else "")).strip()

    filled = await extract_variables(
        text=text,
        variables=variables,
        purpose="voice_autofill",
        session_id=str(user_id) if user_id else "",
    )
    missing = [v["name"] for v in variables if filled.get(v["name"]) in (None, "")]
    return {
        "filled": filled,
        "missing": missing,
        "variables_requested": [v["name"] for v in variables],
        "message": (
            f"Llené {len(variables) - len(missing)} de {len(variables)} variables."
            + (f" Faltan: {', '.join(missing)}." if missing else "")
        ),
    }


async def extract_variables_from_text_tool(args: dict, ctx: dict) -> dict:
    user_id = ctx.get("user_id")
    text = (args.get("text") or "").strip()
    variables = args.get("variables") or []
    if not text:
        return {"error": "Necesito text"}
    if not isinstance(variables, list) or not variables:
        return {"error": "Necesito list de variables"}
    from utils.variable_extractor import extract_variables
    filled = await extract_variables(
        text=text,
        variables=variables,
        purpose="voice_extract",
        session_id=str(user_id) if user_id else "",
    )
    return {"filled": filled}


async def list_intake_forms_tool(args: dict, ctx: dict) -> dict:
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, slug, name, submissions_count
              from intake_forms
             where firm_id = $1::uuid and active = true
             order by created_at desc
             limit 20
            """,
            firm_id,
        )
    return {
        "items": [
            {
                "id": str(r["id"]),
                "slug": r["slug"],
                "name": r["name"],
                "public_url_path": f"/intake/{r['slug']}",
                "submissions_count": int(r["submissions_count"] or 0),
            }
            for r in rows
        ]
    }


async def list_new_submissions_tool(args: dict, ctx: dict) -> dict:
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    limit = min(int(args.get("limit", 10)), 30)
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"items": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select s.id, s.submitter_nombre, s.submitter_email, s.created_at,
                   f.name as form_name, f.id as form_id
              from intake_submissions s
              join intake_forms f on f.id = s.intake_form_id
             where s.firm_id = $1::uuid and s.status = 'new'
             order by s.created_at desc
             limit $2
            """,
            firm_id, limit,
        )
    return {
        "items": [
            {
                "id": str(r["id"]),
                "form_id": str(r["form_id"]),
                "form_name": r["form_name"],
                "submitter": r["submitter_nombre"] or r["submitter_email"] or "Anónimo",
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
        "count": len(rows),
    }
