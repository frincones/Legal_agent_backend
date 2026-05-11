"""User Templates API · plantillas privadas del despacho (Sprint 4).

Tres niveles de templates en el sistema:
  · Globales del producto      → backend/api/legal_templates_api.py (existente)
  · De la firma                → user_templates · owner_id IS NULL
  · Personales del usuario     → user_templates · owner_id = users.id

Endpoints:
  GET    /v1/user-templates/                 → lista (firma + personales)
  GET    /v1/user-templates/{id}             → detalle con content_md
  POST   /v1/user-templates/                 → crea desde markdown
  POST   /v1/user-templates/upload           → crea desde .docx (Docling)
  PATCH  /v1/user-templates/{id}             → edita name/content/variables/default
  DELETE /v1/user-templates/{id}             → elimina

Variables: el content_md puede contener placeholders {{nombre}}, {{nit}}, etc.
El frontend los detecta y abre un dialog para llenarlos antes de insertar
en el canvas.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/user-templates", tags=["user_templates"])


ALLOWED_DOC_TYPES = {
    "tutela", "contestacion", "demanda_laboral", "demanda_civil",
    "derecho_peticion", "recurso_apelacion", "casacion",
    "recurso_reposicion", "dictamen", "memorial", "contrato", "otro",
}

# Allowed file extensions for upload.
ALLOWED_UPLOAD_EXTS = {".docx", ".doc", ".md", ".txt"}

# Variables look like {{nombre_variable}} (alphanumeric + underscore + dot).
_VARIABLE_RE = re.compile(r"\{\{\s*([a-zA-Z][a-zA-Z0-9_.]*)\s*\}\}")


class TemplateRow(BaseModel):
    id: str
    name: str
    doc_type: str
    jurisdiction: str
    target_court: Optional[str]
    is_default_for_type: bool
    owner_id: Optional[str]
    is_personal: bool
    variables: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class TemplateDetail(TemplateRow):
    content_md: str
    metadata: dict = Field(default_factory=dict)


class TemplateCreate(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    doc_type: str
    content_md: str = Field(min_length=4, max_length=120_000)
    target_court: Optional[str] = None
    jurisdiction: str = "CO"
    is_default_for_type: bool = False
    is_personal: bool = False  # if false → shared by firm (owner_id null)
    metadata: dict = Field(default_factory=dict)


class TemplateUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=160)
    doc_type: Optional[str] = None
    content_md: Optional[str] = Field(None, min_length=4, max_length=120_000)
    target_court: Optional[str] = None
    jurisdiction: Optional[str] = None
    is_default_for_type: Optional[bool] = None
    metadata: Optional[dict] = None


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


def _extract_variables(md: str) -> list[str]:
    """Returns unique variable names ({{name}}) in declaration order."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for m in _VARIABLE_RE.finditer(md or ""):
        name = m.group(1)
        if name not in seen_set:
            seen_set.add(name)
            seen.append(name)
    return seen


def _row_to_summary(r, current_user_id: str) -> TemplateRow:
    owner_id = str(r["owner_id"]) if r["owner_id"] else None
    is_personal = owner_id == str(current_user_id)
    return TemplateRow(
        id=str(r["id"]),
        name=r["name"],
        doc_type=r["doc_type"],
        jurisdiction=r["jurisdiction"] or "CO",
        target_court=r["target_court"],
        is_default_for_type=bool(r["is_default_for_type"]),
        owner_id=owner_id,
        is_personal=is_personal,
        variables=list(r["variables"] or []),
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


# ────────────────────────────────────────────────────────────────────
# Endpoints
# ────────────────────────────────────────────────────────────────────


@router.get("/", response_model=list[TemplateRow])
async def list_templates(
    principal: Principal = Depends(get_current_firm),
    doc_type: Optional[str] = Query(None),
    only_mine: bool = False,
):
    where = ["firm_id = $1::uuid"]
    params: list = [principal.firm_id]
    if doc_type:
        params.append(doc_type)
        where.append(f"doc_type = ${len(params)}")
    if only_mine:
        params.append(principal.user_id)
        where.append(f"owner_id = ${len(params)}::uuid")
    sql = f"""
        select id, owner_id, name, doc_type, jurisdiction, target_court,
               is_default_for_type, variables, created_at, updated_at
        from user_templates
        where {' and '.join(where)}
        order by is_default_for_type desc, doc_type asc, name asc
    """
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [_row_to_summary(r, principal.user_id) for r in rows]


@router.get("/{template_id}", response_model=TemplateDetail)
async def get_template(
    template_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            select id, owner_id, name, doc_type, jurisdiction, target_court,
                   is_default_for_type, variables, content_md, metadata,
                   created_at, updated_at
            from user_templates
            where id = $1::uuid and firm_id = $2::uuid
            """,
            template_id, principal.firm_id,
        )
    if not r:
        raise HTTPException(404, "template no encontrado")
    base = _row_to_summary(r, principal.user_id)
    return TemplateDetail(
        **base.model_dump(),
        content_md=r["content_md"],
        metadata=r["metadata"] or {},
    )


@router.post("/", response_model=TemplateDetail, status_code=201)
async def create_template(
    body: TemplateCreate,
    principal: Principal = Depends(get_current_firm),
):
    if body.doc_type not in ALLOWED_DOC_TYPES:
        raise HTTPException(400, f"doc_type inválido: {body.doc_type}")
    variables = _extract_variables(body.content_md)
    tid = str(uuid.uuid4())
    owner = principal.user_id if body.is_personal else None
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        # If marking as default, unset any existing default for the same key.
        if body.is_default_for_type:
            await conn.execute(
                """
                update user_templates set is_default_for_type = false
                where firm_id = $1::uuid
                  and doc_type = $2
                  and coalesce(target_court, '') = coalesce($3::text, '')
                """,
                principal.firm_id, body.doc_type, body.target_court,
            )
        r = await conn.fetchrow(
            """
            insert into user_templates (
              id, firm_id, owner_id, name, doc_type, jurisdiction, target_court,
              content_md, variables, is_default_for_type, metadata
            )
            values (
              $1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7,
              $8, $9::jsonb, $10, $11::jsonb
            )
            returning id, owner_id, name, doc_type, jurisdiction, target_court,
                      is_default_for_type, variables, content_md, metadata,
                      created_at, updated_at
            """,
            tid, principal.firm_id, owner, body.name, body.doc_type,
            body.jurisdiction or "CO", body.target_court,
            body.content_md,
            json.dumps(variables),
            body.is_default_for_type,
            json.dumps(body.metadata),
        )
    base = _row_to_summary(r, principal.user_id)
    return TemplateDetail(
        **base.model_dump(),
        content_md=r["content_md"],
        metadata=r["metadata"] or {},
    )


@router.post("/upload", response_model=TemplateDetail, status_code=201)
async def upload_template(
    file: UploadFile = File(...),
    name: str = Form(...),
    doc_type: str = Form(...),
    target_court: Optional[str] = Form(None),
    is_personal: bool = Form(False),
    is_default_for_type: bool = Form(False),
    principal: Principal = Depends(get_current_firm),
):
    """Receives a .docx (or .md/.txt) and converts it to markdown via the
    existing Docling pipeline, then persists as a template. The docx
    gets stored under storage_path purely for traceability — the
    canonical content lives in user_templates.content_md."""
    if doc_type not in ALLOWED_DOC_TYPES:
        raise HTTPException(400, f"doc_type inválido: {doc_type}")
    filename = file.filename or "template"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_UPLOAD_EXTS:
        raise HTTPException(400, f"extensión no soportada: {ext}")

    # Read bytes and persist to a temp file so the existing reader works.
    data = await file.read()
    if not data:
        raise HTTPException(400, "archivo vacío")
    if len(data) > 8 * 1024 * 1024:  # 8 MB cap
        raise HTTPException(400, "archivo demasiado grande (máx 8 MB)")

    md_content = ""
    if ext in {".md", ".txt"}:
        try:
            md_content = data.decode("utf-8", errors="replace")
        except Exception as e:
            raise HTTPException(400, f"no se pudo decodificar: {e}")
    else:
        # .docx / .doc → Docling
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tf:
            tf.write(data)
            tmp_path = tf.name
        try:
            from ingestion.readers import docling_reader
            md_content, _doc, _meta = docling_reader.read(tmp_path)
        except Exception as e:
            logger.warning("docling read failed: %s", e)
            raise HTTPException(502, f"no se pudo procesar el archivo: {e}")
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    if not md_content or len(md_content.strip()) < 4:
        raise HTTPException(400, "el archivo no contiene texto útil")

    body = TemplateCreate(
        name=name,
        doc_type=doc_type,
        content_md=md_content,
        target_court=target_court,
        is_personal=is_personal,
        is_default_for_type=is_default_for_type,
        metadata={"source_filename": filename, "source_size": len(data)},
    )
    return await create_template(body, principal)


@router.patch("/{template_id}", response_model=TemplateDetail)
async def update_template(
    template_id: str,
    body: TemplateUpdate,
    principal: Principal = Depends(get_current_firm),
):
    if body.doc_type is not None and body.doc_type not in ALLOWED_DOC_TYPES:
        raise HTTPException(400, f"doc_type inválido: {body.doc_type}")
    fields, params = [], []
    if body.name is not None:
        params.append(body.name); fields.append(f"name = ${len(params)}")
    if body.doc_type is not None:
        params.append(body.doc_type); fields.append(f"doc_type = ${len(params)}")
    if body.content_md is not None:
        params.append(body.content_md); fields.append(f"content_md = ${len(params)}")
        params.append(json.dumps(_extract_variables(body.content_md)))
        fields.append(f"variables = ${len(params)}::jsonb")
    if body.target_court is not None:
        params.append(body.target_court); fields.append(f"target_court = ${len(params)}")
    if body.jurisdiction is not None:
        params.append(body.jurisdiction); fields.append(f"jurisdiction = ${len(params)}")
    if body.is_default_for_type is not None:
        params.append(body.is_default_for_type); fields.append(f"is_default_for_type = ${len(params)}")
    if body.metadata is not None:
        params.append(json.dumps(body.metadata)); fields.append(f"metadata = ${len(params)}::jsonb")
    if not fields:
        raise HTTPException(400, "nada que actualizar")
    fields.append("updated_at = now()")
    params.append(template_id); params.append(principal.firm_id)
    sql = f"""
        update user_templates set {', '.join(fields)}
         where id = ${len(params) - 1}::uuid and firm_id = ${len(params)}::uuid
        returning id, owner_id, name, doc_type, jurisdiction, target_court,
                  is_default_for_type, variables, content_md, metadata,
                  created_at, updated_at
    """
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        r = await conn.fetchrow(sql, *params)
    if not r:
        raise HTTPException(404, "template no encontrado")
    base = _row_to_summary(r, principal.user_id)
    return TemplateDetail(
        **base.model_dump(),
        content_md=r["content_md"],
        metadata=r["metadata"] or {},
    )


@router.delete("/{template_id}", status_code=204)
async def delete_template(
    template_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        result = await conn.execute(
            """
            delete from user_templates
             where id = $1::uuid and firm_id = $2::uuid
               and (owner_id is null or owner_id = $3::uuid or
                    exists (select 1 from users
                             where id = $3::uuid and role::text in ('admin','socio_senior')))
            """,
            template_id, principal.firm_id, principal.user_id,
        )
    if result.endswith(" 0"):
        raise HTTPException(404, "template no encontrado o sin permisos")
