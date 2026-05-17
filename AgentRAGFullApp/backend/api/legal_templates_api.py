"""REST endpoints para listar/cargar plantillas legales del Canvas.

GET /v1/legal-templates              → lista combinada · hardcoded Python +
                                       builtin oficiales (user_templates con
                                       firm_id IS NULL) + plantillas de la firma
GET /v1/legal-templates/{kind}       → markdown de la plantilla con placeholders

Las builtin oficiales (Sprint K) provienen de fuentes públicas:
  - Defensoría del Pueblo · tutela
  - Rama Judicial · demanda ejecutiva
  - Cámara de Comercio de Bogotá · estatutos SAS
  - Ley 820/2003 · arrendamiento vivienda urbana

El frontend ya consume este endpoint vía el botón "Plantillas" del canvas
editor y el SlashMenu (cuando el usuario escribe `/`).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/legal-templates", tags=["legal-templates"])


async def _list_db_templates(firm_id: str) -> list[dict[str, Any]]:
    """Lista builtin oficiales (firm_id IS NULL) + plantillas de la firma.
    El usuario sólo ve builtin + las suyas (RLS adicional no necesaria · el
    endpoint ya filtra por firm)."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return []
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, firm_id, name, doc_type, jurisdiction, content_md, metadata
              from user_templates
             where (firm_id is null and owner_id is null) or firm_id = $1::uuid
             order by firm_id nulls first, name
            """,
            firm_id,
        )
    out: list[dict[str, Any]] = []
    for r in rows:
        meta = r["metadata"] or {}
        import json as _json
        if isinstance(meta, str):
            try:
                meta = _json.loads(meta)
            except Exception:
                meta = {}
        is_builtin = r["firm_id"] is None
        specific = (meta.get("specific_type") if isinstance(meta, dict) else None) or r["doc_type"]
        out.append({
            "kind": f"db:{r['id']}",
            "title": r["name"],
            "description": (meta.get("source_name") if isinstance(meta, dict) else None) or "",
            "applicable": specific,
            "source": "oficial" if is_builtin else "firma",
            "source_url": (meta.get("source_url") if isinstance(meta, dict) else None),
        })
    return out


@router.get("/")
async def list_templates_endpoint(
    principal: Principal = Depends(get_current_firm),
):
    """Combina hardcoded Python + builtin oficiales + plantillas firma."""
    from agent.tools.legal_templates import list_templates
    py_tpls = list_templates()
    for t in py_tpls:
        t.setdefault("source", "lexai")
    try:
        db_tpls = await _list_db_templates(principal.firm_id)
    except Exception as e:
        logger.warning("legal-templates DB list failed: %s", e)
        db_tpls = []
    combined = py_tpls + db_tpls
    return {"count": len(combined), "templates": combined}


@router.get("/{kind}")
async def get_template_endpoint(
    kind: str,
    principal: Principal = Depends(get_current_firm),
):
    # Plantilla de BD (Sprint K)
    if kind.startswith("db:"):
        tpl_id = kind[3:]
        from utils.db import get_storage
        storage = await get_storage()
        if not hasattr(storage, "pool"):
            raise HTTPException(503, "storage unavailable")
        async with storage.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                select id, firm_id, name, doc_type, content_md, metadata
                  from user_templates
                 where id = $1::uuid
                   and ((firm_id is null and owner_id is null) or firm_id = $2::uuid)
                """,
                tpl_id, principal.firm_id,
            )
        if not row:
            raise HTTPException(404, f"plantilla '{kind}' no existe o no autorizada")
        meta = row["metadata"] or {}
        import json as _json
        if isinstance(meta, str):
            try:
                meta = _json.loads(meta)
            except Exception:
                meta = {}
        return {
            "kind": kind,
            "title": row["name"],
            "description": (meta.get("source_name") if isinstance(meta, dict) else None) or "",
            "applicable": (meta.get("specific_type") if isinstance(meta, dict) else None) or row["doc_type"],
            "markdown": row["content_md"],
            "source": "oficial" if row["firm_id"] is None else "firma",
            "source_url": (meta.get("source_url") if isinstance(meta, dict) else None),
        }

    # Plantilla hardcoded Python (legacy)
    from agent.tools.legal_templates import TEMPLATES, render_template
    if kind not in TEMPLATES:
        raise HTTPException(404, f"template '{kind}' no existe")
    meta_legacy = TEMPLATES[kind]
    rendered = render_template(kind, facts={})
    return {
        "kind": kind,
        "title": meta_legacy["title"],
        "description": meta_legacy["description"],
        "applicable": meta_legacy["applicable"],
        "markdown": rendered,
        "source": "lexai",
    }
