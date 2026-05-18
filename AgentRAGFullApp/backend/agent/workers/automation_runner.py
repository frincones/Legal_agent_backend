"""Sprint 9 · Automation runner worker.

Ejecuta una rule:
  1. Evalúa conditions (if any)
  2. Ejecuta cada action en orden
  3. Loggea automation_run con status

Para event-driven dispatch (cuando una rule debe correr ante un evento del
sistema, e.g. matter_created), el código llamador debe invocar
`dispatch_event(firm_id, trigger_kind, payload)`. Esto busca rules activas
con ese trigger_kind y las ejecuta. Las llamadas tipo cron (schedule_daily)
las dispara Railway invocando el endpoint /v1/automation/rules/{id}/run-now.

NO se ejecutan rules cross-firm: el filtro firm_id es siempre obligatorio.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


COMPARATORS = {
    "eq": lambda a, b: a == b,
    "neq": lambda a, b: a != b,
    "gt": lambda a, b: (a or 0) > (b or 0),
    "lt": lambda a, b: (a or 0) < (b or 0),
    "gte": lambda a, b: (a or 0) >= (b or 0),
    "lte": lambda a, b: (a or 0) <= (b or 0),
    "contains": lambda a, b: bool(a) and (str(b).lower() in str(a).lower()),
}


def _evaluate_conditions(conditions: list[dict], payload: dict) -> tuple[bool, list[str]]:
    """Devuelve (passed, reasons)."""
    if not conditions:
        return True, []
    reasons: list[str] = []
    for c in conditions:
        field = c.get("field")
        op = c.get("op", "eq")
        expected = c.get("value")
        actual = payload.get(field) if field else None
        comp = COMPARATORS.get(op)
        if not comp:
            reasons.append(f"op desconocido: {op}")
            return False, reasons
        try:
            ok = comp(actual, expected)
        except Exception as e:
            reasons.append(f"error eval {field}: {e}")
            return False, reasons
        if not ok:
            reasons.append(f"{field}={actual!r} {op} {expected!r} → falso")
            return False, reasons
        reasons.append(f"{field} {op} {expected} ✓")
    return True, reasons


async def dry_run_rule(rule_id: str, payload: dict, firm_id: str) -> dict:
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage unavailable"}
    async with storage.pool.acquire() as conn:
        rule = await conn.fetchrow(
            """
            select id, name, trigger_kind, trigger_config, conditions, actions, active
              from automation_rules
             where id = $1::uuid and firm_id = $2::uuid
            """,
            rule_id, firm_id,
        )
    if not rule:
        return {"error": "rule not found"}
    passed, reasons = _evaluate_conditions(rule["conditions"] or [], payload)
    return {
        "rule_id": str(rule["id"]),
        "name": rule["name"],
        "would_execute": passed and bool(rule["active"]),
        "conditions_eval": reasons,
        "actions_planned": rule["actions"] or [],
    }


async def run_rule(rule_id: str, payload: dict, firm_id: str, trigger_event: Optional[str] = None) -> dict:
    """Ejecuta una rule. Idempotencia: si conditions fallan, devuelve skipped."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage unavailable"}
    started = time.time()
    actions_executed: list[dict] = []
    errors: list[str] = []
    skip_reason: Optional[str] = None

    async with storage.pool.acquire() as conn:
        rule = await conn.fetchrow(
            """
            select id, name, trigger_kind, conditions, actions, active
              from automation_rules
             where id = $1::uuid and firm_id = $2::uuid
            """,
            rule_id, firm_id,
        )
    if not rule:
        return {"error": "rule not found"}
    if not rule["active"]:
        skip_reason = "rule_inactive"

    # asyncpg may return JSONB as a string (codec-dependent) · normalize.
    conditions = rule["conditions"]
    if isinstance(conditions, str):
        try:
            conditions = json.loads(conditions)
        except Exception:
            conditions = []
    conditions = conditions or []

    actions = rule["actions"]
    if isinstance(actions, str):
        try:
            actions = json.loads(actions)
        except Exception:
            actions = []
    actions = actions or []

    if not skip_reason:
        passed, reasons = _evaluate_conditions(conditions, payload)
        if not passed:
            skip_reason = "conditions_failed: " + "; ".join(reasons[-3:])

    if skip_reason:
        await _log_run(firm_id, rule_id, trigger_event, payload, [], "skipped", skip_reason, 0)
        return {"status": "skipped", "reason": skip_reason}

    for action in actions:
        try:
            result = await _execute_action(firm_id, action, payload)
            actions_executed.append({"kind": action.get("kind"), "result": result, "ok": True})
        except Exception as e:
            errors.append(f"{action.get('kind')}: {e}")
            actions_executed.append({"kind": action.get("kind"), "error": str(e), "ok": False})

    duration_ms = int((time.time() - started) * 1000)
    status = "success" if not errors else ("partial" if actions_executed else "failed")

    await _log_run(
        firm_id, rule_id, trigger_event, payload, actions_executed,
        status, "; ".join(errors)[:500] if errors else None, duration_ms,
    )

    return {
        "status": status,
        "actions_executed": actions_executed,
        "duration_ms": duration_ms,
        "errors": errors,
    }


async def _log_run(firm_id, rule_id, trigger_event, payload, actions, status, error, duration_ms):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            insert into automation_runs
              (firm_id, rule_id, trigger_event, trigger_payload, actions_executed,
               status, error, duration_ms, completed_at)
            values ($1::uuid, $2::uuid, $3, $4::jsonb, $5::jsonb,
                    $6, $7, $8, now())
            """,
            firm_id, rule_id, trigger_event, json.dumps(payload),
            json.dumps(actions), status, error, duration_ms,
        )
        await conn.execute(
            """
            update automation_rules set
              last_run_at = now(), last_run_status = $2, run_count = run_count + 1,
              updated_at = now()
             where id = $1::uuid
            """,
            rule_id, status,
        )


# ──────────────────────────────────────────────────────────────────────
# Action handlers
# ──────────────────────────────────────────────────────────────────────


async def _execute_action(firm_id: str, action: dict, payload: dict) -> dict:
    kind = action.get("kind")
    params = action.get("params") or {}
    if kind == "create_deadline":
        return await _action_create_deadline(firm_id, params, payload)
    if kind == "create_alert":
        return await _action_create_alert(firm_id, params, payload)
    if kind == "log_note":
        return await _action_log_note(firm_id, params, payload)
    if kind == "tag_matter":
        return await _action_tag_matter(firm_id, params, payload)
    if kind == "send_whatsapp":
        return await _action_send_whatsapp(firm_id, params, payload)
    if kind == "send_email":
        # Email envío real lo haría Sprint posterior; ahora dejamos como info.
        return {"stub": True, "note": "send_email no implementado en Sprint 9 · log only"}
    raise ValueError(f"action kind no soportado: {kind}")


async def _action_create_deadline(firm_id: str, params: dict, payload: dict) -> dict:
    from datetime import date, timedelta
    from utils.db import get_storage
    storage = await get_storage()
    titulo = params.get("titulo") or "Acción automatizada"
    tipo = params.get("tipo") or "otro"
    days = int(params.get("days_from_trigger") or 7)
    matter_id = payload.get("matter_id") or params.get("matter_id")
    if not matter_id:
        return {"skipped": "sin matter_id en payload"}
    fecha = (date.today() + timedelta(days=days)).isoformat()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into matter_deadlines (firm_id, matter_id, titulo, fecha, tipo, origen, completado)
            values ($1::uuid, $2::uuid, $3, $4::date, $5, 'automation', false)
            returning id
            """,
            firm_id, matter_id, titulo, fecha, tipo,
        )
    return {"deadline_id": str(row["id"]), "fecha": fecha}


async def _action_create_alert(firm_id: str, params: dict, payload: dict) -> dict:
    from utils.db import get_storage
    storage = await get_storage()
    severity = params.get("severity", "info")
    if severity not in ("info", "warning", "critical"):
        severity = "info"
    title = params.get("title") or "Alerta automática"
    body = params.get("body") or ""
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into legal_alerts
              (firm_id, target_type, target_ref, kind, severity, title, description, source)
            values ($1::uuid, 'tema', $2, 'cambio_normativo', $3, $4, $5, 'rules')
            returning id
            """,
            firm_id, f"automation:{payload.get('matter_id', 'firm')}", severity, title, body,
        )
    return {"alert_id": str(row["id"])}


async def _action_log_note(firm_id: str, params: dict, payload: dict) -> dict:
    from utils.db import get_storage
    storage = await get_storage()
    matter_id = payload.get("matter_id") or params.get("matter_id")
    body = params.get("body") or ""
    if not matter_id:
        return {"skipped": "sin matter_id"}
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into matter_notes (firm_id, matter_id, body, source)
            values ($1::uuid, $2::uuid, $3, 'automation')
            returning id
            """,
            firm_id, matter_id, body,
        )
    return {"note_id": str(row["id"])}


async def _action_tag_matter(firm_id: str, params: dict, payload: dict) -> dict:
    from utils.db import get_storage
    storage = await get_storage()
    matter_id = payload.get("matter_id") or params.get("matter_id")
    tag = params.get("tag")
    if not (matter_id and tag):
        return {"skipped": "faltan matter_id o tag"}
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            update matters set metadata = coalesce(metadata,'{}'::jsonb) ||
                              jsonb_build_object('tags',
                                coalesce(metadata->'tags','[]'::jsonb) || jsonb_build_array($2::text)
                              )
             where id = $1::uuid and firm_id = $3::uuid
            """,
            matter_id, tag, firm_id,
        )
    return {"tag": tag}


async def _action_send_whatsapp(firm_id: str, params: dict, payload: dict) -> dict:
    """Reutiliza send_whatsapp_tool del Sprint 7."""
    try:
        from api.whatsapp import send_whatsapp_tool
    except Exception as e:
        return {"skipped": f"send_whatsapp no disponible: {e}"}
    to = params.get("to_phone") or payload.get("phone")
    body = params.get("body") or ""
    return await send_whatsapp_tool({"to_phone": to, "body": body}, {"firm_id": firm_id})


# ──────────────────────────────────────────────────────────────────────
# Event dispatcher (para usar desde otros endpoints)
# ──────────────────────────────────────────────────────────────────────


async def dispatch_event(firm_id: str, trigger_kind: str, payload: dict) -> dict:
    """Busca reglas activas con el trigger y las ejecuta. Fire-and-forget seguro."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage unavailable"}
    async with storage.pool.acquire() as conn:
        rules = await conn.fetch(
            """
            select id from automation_rules
             where firm_id = $1::uuid and trigger_kind = $2 and active = true
            """,
            firm_id, trigger_kind,
        )
    results = []
    for r in rules:
        try:
            result = await run_rule(str(r["id"]), payload, firm_id, trigger_event=trigger_kind)
            results.append({"rule_id": str(r["id"]), **result})
        except Exception as e:
            logger.warning("dispatch rule %s failed: %s", r["id"], e)
            results.append({"rule_id": str(r["id"]), "error": str(e)})
    return {"trigger": trigger_kind, "rules_fired": len(rules), "results": results}
