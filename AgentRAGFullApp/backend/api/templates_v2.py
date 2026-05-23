"""Sprint M2 · Router /v1/templates · expone catálogo de TemplateDef."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from lex.templates import registry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/templates", tags=["templates-v2"])


async def _require_session(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing_bearer_token")
    return {"token": auth[7:]}


class TemplateListItem(BaseModel):
    id: str
    nombre: str
    jurisdiccion: str
    materia: str
    description: str
    sections_count: int


@router.get("", response_model=list[TemplateListItem])
async def list_templates(
    jurisdiccion: Optional[str] = None,
    _claims: dict = Depends(_require_session),
):
    """Lista todos los templates disponibles. Filtro opcional por jurisdicción."""
    templates = registry.list_all()
    if jurisdiccion:
        templates = [t for t in templates if t.jurisdiccion == jurisdiccion]
    return [
        TemplateListItem(
            id=t.id, nombre=t.nombre, jurisdiccion=t.jurisdiccion,
            materia=t.materia, description=t.description,
            sections_count=len(t.sections_plan),
        )
        for t in templates
    ]


@router.get("/{template_id}/preview")
async def preview_template(
    template_id: str,
    _claims: dict = Depends(_require_session),
):
    """Devuelve la TemplateDef completa para preview en UI antes de generar."""
    t = registry.get(template_id)
    if not t:
        raise HTTPException(status_code=404, detail=f"template_not_found:{template_id}")
    return t.to_dict()


@router.get("/{template_id}/required-data")
async def required_data_schema(
    template_id: str,
    _claims: dict = Depends(_require_session),
):
    """Schema mínimo de datos requeridos (para form dinámico)."""
    t = registry.get(template_id)
    if not t:
        raise HTTPException(status_code=404, detail=f"template_not_found:{template_id}")
    return {
        "template_id": template_id,
        "required_data": t.required_data,
    }
