"""REST endpoints para listar/cargar plantillas legales del Canvas.

GET /v1/legal-templates              → lista (kind, title, description, applicable)
GET /v1/legal-templates/{kind}       → markdown de la plantilla con placeholders
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from utils.auth import Principal, get_current_firm

router = APIRouter(prefix="/v1/legal-templates", tags=["legal-templates"])


@router.get("/")
async def list_templates_endpoint(
    principal: Principal = Depends(get_current_firm),
):
    from agent.tools.legal_templates import list_templates
    return {"count": len(list_templates()), "templates": list_templates()}


@router.get("/{kind}")
async def get_template_endpoint(
    kind: str,
    principal: Principal = Depends(get_current_firm),
):
    from agent.tools.legal_templates import TEMPLATES, render_template
    if kind not in TEMPLATES:
        raise HTTPException(404, f"template '{kind}' no existe")
    meta = TEMPLATES[kind]
    # Renderizar con valores vacíos → muestra placeholders [...]
    rendered = render_template(kind, facts={})
    return {
        "kind": kind,
        "title": meta["title"],
        "description": meta["description"],
        "applicable": meta["applicable"],
        "markdown": rendered,
    }
