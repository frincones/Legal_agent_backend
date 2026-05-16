"""Sprint E · Router /v1/admin/saas/skills · gestión global de skills (admin-only).

Permite a admin SaaS:
  - CRUD skills builtin (firm_id=null)
  - Versionar (publish/draft/deprecate)
  - Editar SKILL.md raw
  - Test runner con input mock
  - Ver métricas de uso por firma
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm
from utils.admin_guard import require_saas_admin
from utils.skill_runner import run_skill

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/admin/saas", tags=["saas-admin"])


# ─────────────────────────────────────────────────────────────────────
# SKILLS · CRUD admin global
# ─────────────────────────────────────────────────────────────────────

class SkillCreateBody(BaseModel):
    command: str = Field(..., pattern=r"^/[a-z0-9/-]+$")
    name: str
    description: Optional[str] = None
    category: str = Field(default="other")
    system_prompt: str
    output_schema: Optional[dict[str, Any]] = None
    references_md: Optional[str] = None
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    jurisdiction: str = "co"
    user_invocable: bool = True
    status: str = Field(default="draft", pattern="^(draft|published)$")


@router.get("/skills")
async def list_all_skills(
    status: Optional[str] = None,
    category: Optional[str] = None,
    _admin: Principal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    where = ["firm_id is null"]
    params: list[Any] = []
    if status:
        where.append(f"status = ${len(params) + 1}")
        params.append(status)
    if category:
        where.append(f"category = ${len(params) + 1}")
        params.append(category)
    q = (
        "select id, command, name, description, category, jurisdiction, status, "
        "version, user_invocable, created_at, updated_at "
        "from firm_skills where " + " and ".join(where) +
        " order by category, command, version desc"
    )
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(q, *params)
    return [
        {
            **dict(r),
            "id": str(r["id"]),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]


@router.post("/skills")
async def create_builtin_skill(
    body: SkillCreateBody,
    principal: Principal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        # Buscar última versión del mismo command (builtin)
        last = await conn.fetchrow(
            "select coalesce(max(version), 0) as v from firm_skills "
            "where firm_id is null and command = $1",
            body.command,
        )
        next_version = (last["v"] or 0) + 1
        row = await conn.fetchrow(
            """
            insert into firm_skills
              (firm_id, command, name, description, category,
               frontmatter, system_prompt, output_schema, references_md,
               user_invocable, jurisdiction, version, status, created_by)
            values (null, $1, $2, $3, $4, $5::jsonb, $6, $7::jsonb, $8,
                    $9, $10, $11, $12, $13::uuid)
            returning id, version
            """,
            body.command, body.name, body.description, body.category,
            json.dumps(body.frontmatter), body.system_prompt,
            json.dumps(body.output_schema) if body.output_schema else None,
            body.references_md, body.user_invocable, body.jurisdiction,
            next_version, body.status, principal.user_id,
        )
    return {"id": str(row["id"]), "version": row["version"], "status": body.status}


@router.patch("/skills/{skill_id}")
async def update_builtin_skill(
    skill_id: UUID,
    body: SkillCreateBody,
    _admin: Principal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            update firm_skills
               set name = $1, description = $2, category = $3,
                   frontmatter = $4::jsonb, system_prompt = $5,
                   output_schema = $6::jsonb, references_md = $7,
                   user_invocable = $8, jurisdiction = $9, status = $10
             where id = $11::uuid and firm_id is null
             returning id
            """,
            body.name, body.description, body.category,
            json.dumps(body.frontmatter), body.system_prompt,
            json.dumps(body.output_schema) if body.output_schema else None,
            body.references_md, body.user_invocable, body.jurisdiction,
            body.status, skill_id,
        )
    if not r:
        raise HTTPException(404, "builtin skill not found")
    return {"id": str(r["id"]), "ok": True}


@router.delete("/skills/{skill_id}")
async def archive_builtin_skill(
    skill_id: UUID,
    _admin: Principal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        r = await conn.fetchrow(
            """update firm_skills set status = 'archived'
                where id = $1::uuid and firm_id is null returning id""",
            skill_id,
        )
    if not r:
        raise HTTPException(404, "builtin skill not found")
    return {"id": str(r["id"]), "status": "archived"}


# ─────────────────────────────────────────────────────────────────────
# SKILLS · Test runner (sin afectar firma real)
# ─────────────────────────────────────────────────────────────────────

class SkillTestBody(BaseModel):
    command: str
    test_input: dict[str, Any] = Field(default_factory=dict)
    test_firm_id: Optional[UUID] = None  # default: usa firma del admin


@router.post("/skills/test")
async def test_skill(
    body: SkillTestBody,
    principal: Principal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    test_firm = str(body.test_firm_id) if body.test_firm_id else principal.firm_id
    result = await run_skill(
        storage.pool,
        firm_id=test_firm,
        user_id=principal.user_id,
        command=body.command,
        input_data=body.test_input,
    )
    return result


# ─────────────────────────────────────────────────────────────────────
# HOOKS · Admin
# ─────────────────────────────────────────────────────────────────────

@router.get("/hooks")
async def list_hooks(_admin: Principal = Depends(require_saas_admin)):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            "select * from skill_hooks order by order_index asc"
        )
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        for k in ("created_at", "updated_at"):
            if d.get(k):
                d[k] = d[k].isoformat()
        if isinstance(d.get("config"), str):
            try:
                d["config"] = json.loads(d["config"])
            except Exception:
                d["config"] = {}
        out.append(d)
    return out


class HookUpdateBody(BaseModel):
    enabled: Optional[bool] = None
    order_index: Optional[int] = None
    decision_mode: Optional[str] = None
    applies_to: Optional[list[str]] = None
    config: Optional[dict[str, Any]] = None


@router.patch("/hooks/{hook_id}")
async def update_hook(
    hook_id: UUID,
    body: HookUpdateBody,
    _admin: Principal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    sets = []
    params: list[Any] = []
    if body.enabled is not None:
        params.append(body.enabled)
        sets.append(f"enabled = ${len(params)}")
    if body.order_index is not None:
        params.append(body.order_index)
        sets.append(f"order_index = ${len(params)}")
    if body.decision_mode is not None:
        params.append(body.decision_mode)
        sets.append(f"decision_mode = ${len(params)}")
    if body.applies_to is not None:
        params.append(body.applies_to)
        sets.append(f"applies_to = ${len(params)}")
    if body.config is not None:
        params.append(json.dumps(body.config))
        sets.append(f"config = ${len(params)}::jsonb")
    if not sets:
        raise HTTPException(400, "Nada para actualizar")
    params.append(hook_id)
    q = f"update skill_hooks set {', '.join(sets)}, updated_at = now() where id = ${len(params)}::uuid returning id"
    async with storage.pool.acquire() as conn:
        r = await conn.fetchrow(q, *params)
    if not r:
        raise HTTPException(404, "hook not found")
    return {"id": str(r["id"]), "ok": True}


# ─────────────────────────────────────────────────────────────────────
# SKILLS METRICS · uso por firma
# ─────────────────────────────────────────────────────────────────────

@router.get("/skills/metrics")
async def skills_metrics(
    days: int = 30,
    _admin: Principal = Depends(require_saas_admin),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        by_command = await conn.fetch(
            f"""
            select command,
                   count(*) as executions,
                   count(*) filter (where status = 'success') as success_count,
                   count(*) filter (where status = 'error') as error_count,
                   count(*) filter (where status = 'blocked_by_hook') as blocked_count,
                   avg(duration_ms)::int as avg_duration_ms,
                   sum(tokens_input) as total_tokens_in,
                   sum(tokens_output) as total_tokens_out,
                   sum(cost_usd_cents) as total_cost_cents
              from skill_executions
             where started_at > now() - ($1 || ' days')::interval
             group by command
             order by executions desc
            """,
            str(days),
        )
        by_firm = await conn.fetch(
            f"""
            select firm_id::text as firm_id,
                   count(*) as executions,
                   count(distinct user_id) as unique_users,
                   sum(cost_usd_cents) as total_cost_cents
              from skill_executions
             where started_at > now() - ($1 || ' days')::interval
             group by firm_id
             order by executions desc limit 50
            """,
            str(days),
        )
    return {
        "window_days": days,
        "by_command": [dict(r) for r in by_command],
        "by_firm": [dict(r) for r in by_firm],
    }


# ─────────────────────────────────────────────────────────────────────
# PLAYBOOK TEMPLATES · plantillas global
# ─────────────────────────────────────────────────────────────────────

@router.get("/playbook-templates")
async def list_playbook_templates(_admin: Principal = Depends(require_saas_admin)):
    """Lista playbooks ejemplo · una firma puede adoptar uno como base.
    Implementación inicial: builtin hardcoded · expandir a tabla en próximo sprint."""
    return [
        {
            "id": "default-co",
            "name": "Despacho colombiano · Default",
            "description": "Plantilla para despachos pequeños/medianos en Colombia",
            "jurisdiction_default": "co",
            "redline_style": "tracked",
            "tone": "formal",
            "forbidden_terms": ["exclusividad perpetua", "renuncia total derechos"],
            "required_clauses": ["objeto", "plazo", "habeas data", "terminación"],
            "preferred_clauses": {
                "habeas_data": (
                    "El TITULAR autoriza al RESPONSABLE el tratamiento de datos personales "
                    "conforme a la Ley 1581 de 2012 y el Decreto 1377 de 2013…"
                ),
            },
        },
        {
            "id": "litigation-co",
            "name": "Litigation Colombia",
            "description": "Para firmas con foco litigios civiles/laborales",
            "jurisdiction_default": "co",
            "redline_style": "tracked",
            "tone": "aggressive",
            "forbidden_terms": [],
            "required_clauses": ["jurisdicción competente", "ley aplicable"],
        },
    ]


# ─────────────────────────────────────────────────────────────────────
# REDLINE POLICIES · forbidden/required global heredables
# ─────────────────────────────────────────────────────────────────────

@router.get("/redline-policies")
async def list_redline_policies(_admin: Principal = Depends(require_saas_admin)):
    """Políticas globales que admin SaaS puede definir como inheritable defaults."""
    # Inicial: returnar las plantillas de playbook-templates como base de policies
    return {
        "forbidden_terms_global": [],
        "required_clauses_global": [],
        "notes": "Editar via PUT · próximo sprint expande con tabla dedicada",
    }


# ─────────────────────────────────────────────────────────────────────
# JURISDICTIONS · gestión jurisdicciones disponibles
# ─────────────────────────────────────────────────────────────────────

@router.get("/jurisdictions")
async def list_jurisdictions(_admin: Principal = Depends(require_saas_admin)):
    return [
        {"code": "co", "name": "Colombia", "active": True, "default": True},
        {"code": "mx", "name": "México", "active": False},
        {"code": "pe", "name": "Perú", "active": False},
        {"code": "cl", "name": "Chile", "active": False},
        {"code": "ar", "name": "Argentina", "active": False},
        {"code": "us", "name": "Estados Unidos", "active": False},
    ]
