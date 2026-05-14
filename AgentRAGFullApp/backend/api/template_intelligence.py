"""Sprint 19 · Template intelligence · auto-fill + extract.

  POST /v1/template-ai/extract-variables
       body: { text: str, variables: [{name, kind, hint?, options?}] }
       → { filled: {var: value, ...} }

  POST /v1/template-ai/parse-template
       body: { body: str }
       → { variables: ["nombre", "cuantia", ...] }
            (los nombres encontrados en {{var}}; útil para preview)

  POST /v1/template-ai/autofill-from-matter
       body: { matter_id, template_body, extra_text? }
       → { filled: {var: value}, missing: [vars sin determinar] }
       Usa el contexto del matter (cliente, partes, documentos resumen)
       como fuente para extraer.

  POST /v1/template-ai/render
       body: { template_body, values: {var: value} }
       → { rendered: str }
       Reemplazo literal de {{var}} → value. Si el var no está en values,
       deja la mark `{{var}}` para que el usuario sepa que falta.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/template-ai", tags=["template_intelligence"])


class ExtractIn(BaseModel):
    text: str = Field(..., min_length=1)
    variables: list[dict] = Field(default_factory=list)


class ParseIn(BaseModel):
    body: str = Field(..., min_length=1)


class AutofillIn(BaseModel):
    matter_id: str
    template_body: Optional[str] = None        # si se da, infiere variables
    variables: Optional[list[dict]] = None     # explícitas (override)
    extra_text: Optional[str] = None           # añadido al contexto


class RenderIn(BaseModel):
    template_body: str
    values: dict[str, Any] = Field(default_factory=dict)


@router.post("/extract-variables")
async def extract_variables(
    body: ExtractIn,
    principal: Principal = Depends(get_current_firm),
):
    from utils.variable_extractor import extract_variables as extract
    filled = await extract(
        text=body.text,
        variables=body.variables,
        purpose="template_extract_endpoint",
        session_id=str(principal.user_id) if principal.user_id else "",
    )
    return {"filled": filled}


@router.post("/parse-template")
async def parse_template(
    body: ParseIn,
    _: Principal = Depends(get_current_firm),
):
    from utils.variable_extractor import parse_template_variables
    return {"variables": parse_template_variables(body.body)}


@router.post("/autofill-from-matter")
async def autofill_from_matter(
    body: AutofillIn,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    from utils.variable_extractor import (
        extract_variables, parse_template_variables, gather_matter_context_text,
    )
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")

    # 1) Determinar lista de variables
    variables: list[dict] = []
    if body.variables:
        variables = list(body.variables)
    elif body.template_body:
        names = parse_template_variables(body.template_body)
        variables = [{"name": n, "kind": _guess_kind(n)} for n in names]
    if not variables:
        return {"filled": {}, "missing": []}

    # 2) Compilar texto fuente del matter context
    async with storage.pool.acquire() as conn:
        ctx = await gather_matter_context_text(conn, principal.firm_id, body.matter_id)
    if not ctx and not body.extra_text:
        raise HTTPException(404, "Caso no encontrado o sin datos para autofill")
    full_text = (ctx + ("\n\n# Texto adicional\n" + body.extra_text if body.extra_text else "")).strip()

    # 3) Extract
    filled = await extract_variables(
        text=full_text,
        variables=variables,
        purpose="autofill_from_matter",
        session_id=str(principal.user_id) if principal.user_id else "",
    )
    missing = [v["name"] for v in variables if filled.get(v["name"]) in (None, "")]
    return {"filled": filled, "missing": missing, "variables_requested": [v["name"] for v in variables]}


@router.post("/render")
async def render(
    body: RenderIn,
    _: Principal = Depends(get_current_firm),
):
    return {"rendered": render_template(body.template_body, body.values)}


def render_template(template_body: str, values: dict) -> str:
    """Reemplazo literal `{{var}}` → str(value). Si falta una var, deja el
    placeholder visible para que el usuario sepa qué llenar manualmente."""
    if not template_body:
        return ""

    def repl(m: re.Match) -> str:
        name = m.group(1).strip()
        v = values.get(name)
        if v is None or v == "":
            return m.group(0)  # leave as-is
        if isinstance(v, float):
            # números: tabular sin decimales si es entero implícito
            return f"{int(v)}" if v.is_integer() else f"{v}"
        return str(v)

    return re.sub(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}", repl, template_body)


def _guess_kind(name: str) -> str:
    """Heurística por el nombre de la variable: usa el sufijo/keyword."""
    lname = (name or "").lower()
    if any(s in lname for s in ("cuantia", "monto", "valor", "salario", "honorario", "indemnizacion", "amount", "cop")):
        return "number"
    if any(s in lname for s in ("fecha", "date")):
        return "date"
    if any(s in lname for s in ("acepta", "consent", "is_", "es_")):
        return "checkbox"
    if "email" in lname:
        return "text"
    return "text"
