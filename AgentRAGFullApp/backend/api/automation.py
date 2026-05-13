"""Sprint 9 · Automation rules API · workflow automation.

Modelo:
  Rule = { trigger_kind, trigger_config, conditions[], actions[] }

Triggers soportados:
  - matter_created
  - matter_stage_changed       (config.from, config.to)
  - deadline_due_in            (config.days)
  - client_created
  - invoice_overdue            (config.days)
  - lead_stage_changed
  - schedule_daily
  - schedule_weekly

Actions soportadas:
  - create_deadline            (params: titulo, tipo, days_from_trigger)
  - send_email                 (params: to, subject, body)
  - send_whatsapp              (params: to_phone, body)
  - create_alert               (params: severity, title, body)
  - tag_matter                 (params: tag)
  - log_note                   (params: body)

Endpoints:
  GET    /v1/automation/rules
  POST   /v1/automation/rules
  GET    /v1/automation/rules/{id}
  PATCH  /v1/automation/rules/{id}
  DELETE /v1/automation/rules/{id}
  POST   /v1/automation/rules/{id}/test         · dry-run
  POST   /v1/automation/rules/{id}/run-now      · ejecuta ahora
  GET    /v1/automation/runs                    · audit log
  GET    /v1/automation/triggers                · catálogo
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/automation", tags=["automation"])


TRIGGER_KINDS = [
    "matter_created", "matter_stage_changed",
    "deadline_due_in", "client_created",
    "invoice_overdue", "lead_stage_changed",
    "schedule_daily", "schedule_weekly",
]
ACTION_KINDS = [
    "create_deadline", "send_email", "send_whatsapp",
    "create_alert", "tag_matter", "log_note",
]


@router.get("/triggers")
async def list_triggers():
    return {
        "triggers": [
            {"kind": "matter_created", "label": "Caso creado", "config_schema": {}},
            {"kind": "matter_stage_changed", "label": "Caso cambia de etapa", "config_schema": {"from": "string?", "to": "string?"}},
            {"kind": "deadline_due_in", "label": "Plazo próximo a vencer", "config_schema": {"days": "int"}},
            {"kind": "client_created", "label": "Cliente nuevo registrado", "config_schema": {}},
            {"kind": "invoice_overdue", "label": "Factura vencida", "config_schema": {"days": "int"}},
            {"kind": "lead_stage_changed", "label": "Lead cambia de etapa", "config_schema": {"to_stage_name": "string?"}},
            {"kind": "schedule_daily", "label": "Diario (cron)", "config_schema": {"hour": "int (0-23)"}},
            {"kind": "schedule_weekly", "label": "Semanal", "config_schema": {"weekday": "int (0=lun)"}},
        ],
        "actions": [
            {"kind": "create_deadline", "label": "Crear plazo", "params": {"titulo": "string", "tipo": "string", "days_from_trigger": "int"}},
            {"kind": "send_email", "label": "Enviar email (stub)", "params": {"to": "string", "subject": "string", "body": "string"}},
            {"kind": "send_whatsapp", "label": "Enviar WhatsApp", "params": {"to_phone": "string", "body": "string"}},
            {"kind": "create_alert", "label": "Crear alerta interna", "params": {"severity": "info|warning|critical", "title": "string", "body": "string"}},
            {"kind": "tag_matter", "label": "Etiquetar caso", "params": {"tag": "string"}},
            {"kind": "log_note", "label": "Agregar nota al caso", "params": {"body": "string"}},
        ],
    }


def _serialize(r) -> dict:
    return {
        "id": str(r["id"]),
        "name": r["name"],
        "description": r["description"],
        "trigger_kind": r["trigger_kind"],
        "trigger_config": r["trigger_config"],
        "conditions": r["conditions"],
        "actions": r["actions"],
        "active": r["active"],
        "last_run_at": r["last_run_at"].isoformat() if r["last_run_at"] else None,
        "last_run_status": r["last_run_status"],
        "run_count": r["run_count"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


@router.get("/rules")
async def list_rules(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, name, description, trigger_kind, trigger_config,
                   conditions, actions, active, last_run_at, last_run_status,
                   run_count, created_at
              from automation_rules
             where firm_id = $1::uuid
             order by created_at desc
            """,
            principal.firm_id,
        )
    return {"count": len(rows), "items": [_serialize(r) for r in rows]}


class CreateRequest(BaseModel):
    name: str = Field(min_length=2)
    description: Optional[str] = None
    trigger_kind: str
    trigger_config: dict = Field(default_factory=dict)
    conditions: list[dict] = Field(default_factory=list)
    actions: list[dict] = Field(default_factory=list)
    active: bool = True


def _validate_rule(body: CreateRequest):
    if body.trigger_kind not in TRIGGER_KINDS:
        raise HTTPException(400, f"trigger_kind inválido: {body.trigger_kind}")
    for a in body.actions:
        k = a.get("kind")
        if k not in ACTION_KINDS:
            raise HTTPException(400, f"action.kind inválido: {k}")
    if not body.actions:
        raise HTTPException(400, "actions no puede estar vacío")


@router.post("/rules")
async def create_rule(
    body: CreateRequest,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior", "socio_junior"):
        raise HTTPException(403, "Solo socios/admin pueden crear reglas")
    _validate_rule(body)
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into automation_rules
              (firm_id, name, description, trigger_kind, trigger_config,
               conditions, actions, active, created_by)
            values ($1::uuid, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8, $9::uuid)
            returning id, name, description, trigger_kind, trigger_config,
                      conditions, actions, active, last_run_at, last_run_status,
                      run_count, created_at
            """,
            principal.firm_id, body.name, body.description, body.trigger_kind,
            json.dumps(body.trigger_config), json.dumps(body.conditions),
            json.dumps(body.actions), body.active, principal.user_id,
        )
    return _serialize(row)


@router.get("/rules/{rule_id}")
async def get_rule(rule_id: str, principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, name, description, trigger_kind, trigger_config,
                   conditions, actions, active, last_run_at, last_run_status,
                   run_count, created_at
              from automation_rules
             where id = $1::uuid and firm_id = $2::uuid
            """,
            rule_id, principal.firm_id,
        )
    if not row:
        raise HTTPException(404, "not found")
    return _serialize(row)


class PatchRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    trigger_kind: Optional[str] = None
    trigger_config: Optional[dict] = None
    conditions: Optional[list[dict]] = None
    actions: Optional[list[dict]] = None
    active: Optional[bool] = None


@router.patch("/rules/{rule_id}")
async def patch_rule(
    rule_id: str,
    body: PatchRequest,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior", "socio_junior"):
        raise HTTPException(403, "Solo socios/admin")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    fields, params = [], [rule_id, principal.firm_id]
    if body.name is not None:
        params.append(body.name); fields.append(f"name = ${len(params)}")
    if body.description is not None:
        params.append(body.description); fields.append(f"description = ${len(params)}")
    if body.trigger_kind is not None:
        if body.trigger_kind not in TRIGGER_KINDS:
            raise HTTPException(400, "trigger_kind inválido")
        params.append(body.trigger_kind); fields.append(f"trigger_kind = ${len(params)}")
    if body.trigger_config is not None:
        params.append(json.dumps(body.trigger_config)); fields.append(f"trigger_config = ${len(params)}::jsonb")
    if body.conditions is not None:
        params.append(json.dumps(body.conditions)); fields.append(f"conditions = ${len(params)}::jsonb")
    if body.actions is not None:
        for a in body.actions:
            if a.get("kind") not in ACTION_KINDS:
                raise HTTPException(400, f"action.kind inválido: {a.get('kind')}")
        params.append(json.dumps(body.actions)); fields.append(f"actions = ${len(params)}::jsonb")
    if body.active is not None:
        params.append(body.active); fields.append(f"active = ${len(params)}")
    if not fields:
        raise HTTPException(400, "nada que actualizar")
    sql = f"""
        update automation_rules set {', '.join(fields)}, updated_at = now()
         where id = $1::uuid and firm_id = $2::uuid
         returning id, name, description, trigger_kind, trigger_config,
                   conditions, actions, active, last_run_at, last_run_status,
                   run_count, created_at
    """
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(sql, *params)
    if not row:
        raise HTTPException(404, "not found")
    return _serialize(row)


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: str, principal: Principal = Depends(get_current_firm)):
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "delete from automation_rules where id = $1::uuid and firm_id = $2::uuid",
            rule_id, principal.firm_id,
        )
    return {"deleted": True}


class TestRequest(BaseModel):
    payload: dict = Field(default_factory=dict)


@router.post("/rules/{rule_id}/test")
async def test_rule(
    rule_id: str,
    body: TestRequest,
    principal: Principal = Depends(get_current_firm),
):
    """Dry-run: evalúa conditions + lista acciones que se ejecutarían, SIN escribir."""
    from agent.workers.automation_runner import dry_run_rule
    return await dry_run_rule(rule_id, body.payload, str(principal.firm_id))


@router.post("/rules/{rule_id}/run-now")
async def run_now(rule_id: str, principal: Principal = Depends(get_current_firm)):
    if principal.role not in ("admin", "socio_senior", "socio_junior"):
        raise HTTPException(403, "Solo socios/admin")
    from agent.workers.automation_runner import run_rule
    return await run_rule(rule_id, {"manual": True, "user_id": str(principal.user_id)}, str(principal.firm_id))


@router.get("/runs")
async def list_runs(
    rule_id: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    where = ["firm_id = $1::uuid"]
    params: list = [principal.firm_id]
    if rule_id:
        params.append(rule_id); where.append(f"rule_id = ${len(params)}::uuid")
    params.append(limit)
    sql = f"""
        select id, rule_id, trigger_event, trigger_payload, actions_executed,
               status, error, duration_ms, started_at, completed_at
          from automation_runs
         where {' and '.join(where)}
         order by started_at desc
         limit ${len(params)}
    """
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]),
                "rule_id": str(r["rule_id"]),
                "trigger_event": r["trigger_event"],
                "trigger_payload": r["trigger_payload"],
                "actions_executed": r["actions_executed"],
                "status": r["status"],
                "error": r["error"],
                "duration_ms": r["duration_ms"],
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
            }
            for r in rows
        ],
    }


# ════════════════════════════════════════════════════════════════════════
# Voice tool
# ════════════════════════════════════════════════════════════════════════


async def run_automation_tool(args: dict, ctx: dict) -> dict:
    """Voice: 'LexAI, corre la automatización X'."""
    firm_id = ctx.get("firm_id")
    rule_id = args.get("rule_id")
    if not (firm_id and rule_id):
        return {"error": "firm_id y rule_id requeridos"}
    from agent.workers.automation_runner import run_rule
    return await run_rule(rule_id, {"manual": True, "via": "voice"}, firm_id)
