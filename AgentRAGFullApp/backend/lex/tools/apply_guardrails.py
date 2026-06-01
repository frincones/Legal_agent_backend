"""Tool · apply_guardrails · ejecuta los 5 guardrails universales G1-G5.

Sprint M21.S2. Wrapper sobre tabla firm_guardrails (Sprint 1).

Anthropic Claude-for-Legal define 5 guardrails universales que se aplican a TODO
output legal antes de entregarlo al usuario:

  G1 (destination_check):    bloquea outputs destinados a courts/regulators salvo
                             que el firm tenga aprobacion explicita.
  G2 (work_product_header):  fuerza header tipo "Privileged Attorney-Client Work
                             Product / Draft" al inicio de documentos.
  G3 (non_lawyer_gates):     bloquea acciones legales si el usuario actual no es
                             abogado certificado (rol != 'lawyer').
  G4 (attorney_contact):     anexa block de attorney_contact (nombre/email del
                             responsable) al final si esta configurado.
  G5 (escalation_rules):     dispara notification (slack/email) si el output cae
                             en una regla de escalamiento (e.g., monto > $X,
                             menores de edad, derecho penal sin licencia, etc).

Este tool NO bloquea por defecto en runtime — devuelve un guardrails_report con
findings + suggested_actions. El Brain decide si reescribe el output o avisa.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from .base import ToolContext, ToolDef

logger = logging.getLogger(__name__)


# Defaults razonables si el firm no configuro firm_guardrails.
DEFAULT_GUARDRAILS = {
    "destination_check_enabled": True,
    "work_product_header_text": "PRIVILEGIADO · TRABAJO ABOGADO-CLIENTE · BORRADOR",
    "non_lawyer_gates": True,
    "attorney_contact_user_id": None,
    "escalation_rules": [
        {"id": "monto_alto", "trigger_regex": r"\$\s*\d{9,}", "channel": "email", "severity": "high",
         "message": "Output menciona monto >= $1.000.000.000 — revision senior recomendada"},
        {"id": "menores", "trigger_regex": r"\bmenor(?:es)?\s+de\s+edad\b", "channel": "email",
         "severity": "high", "message": "Output involucra menores de edad — verificar Ley 1098/2006"},
        {"id": "penal", "trigger_regex": r"\b(?:imputaci[oó]n|condena|prisi[oó]n|delito)\b",
         "channel": "slack", "severity": "medium",
         "message": "Output con terminologia penal — confirmar habilitacion en area penal"},
    ],
    "notification_channels": {"slack": None, "email": None},
}


class ApplyGuardrailsTool(ToolDef):
    name = "apply_guardrails"
    description = (
        "★ USAR antes de entregar un output final al usuario (documento legal, "
        "respuesta, dictamen). Ejecuta los 5 guardrails universales G1-G5 "
        "configurados por el firm: destination_check, work_product_header, "
        "non_lawyer_gates, attorney_contact, escalation_rules. NO bloquea por "
        "defecto — devuelve guardrails_report con findings + suggested_actions. "
        "El Brain decide si reescribe el output o solo lo anota."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "draft_text": {
                "type": "string",
                "description": "Texto/markdown del output que sera evaluado",
            },
            "destination": {
                "type": "string",
                "enum": ["internal", "client", "opposing_counsel", "court", "regulator", "public"],
                "default": "client",
                "description": "Destino previsto del output (afecta G1 destination_check)",
            },
            "actor_role": {
                "type": "string",
                "enum": ["lawyer", "paralegal", "assistant", "client", "unknown"],
                "default": "lawyer",
                "description": "Rol del usuario que solicito el output (afecta G3 non_lawyer_gates)",
            },
            "matter_id": {
                "type": "string",
                "description": "(Opcional) matter_id para registrar el guardrail check en matter_history",
            },
        },
        "required": ["draft_text"],
    }
    timeout_seconds = 5.0

    def __init__(self, pool=None, **_: Any):
        self.pool = pool

    async def run(
        self,
        ctx: ToolContext,
        draft_text: str,
        destination: str = "client",
        actor_role: str = "lawyer",
        matter_id: Optional[str] = None,
    ) -> dict:
        pool = self.pool or ctx.pool
        config = await self._load_config(pool, ctx)

        findings = []
        suggested_actions = []
        block = False

        # ─── G1 · destination_check ──────────────────────────
        if config.get("destination_check_enabled") and destination in ("court", "regulator"):
            findings.append({
                "guardrail": "G1_destination_check",
                "severity": "high",
                "message": f"Output destinado a {destination!r} — requiere aprobacion senior antes de envio externo",
            })
            suggested_actions.append({
                "action": "require_human_approval",
                "reason": "destination=court/regulator",
            })

        # ─── G2 · work_product_header ────────────────────────
        header = config.get("work_product_header_text")
        if header and not draft_text.lstrip().lower().startswith(header.lower()[:20]):
            findings.append({
                "guardrail": "G2_work_product_header",
                "severity": "low",
                "message": "Falta header de work product al inicio del documento",
            })
            suggested_actions.append({
                "action": "prepend_header",
                "text": header,
            })

        # ─── G3 · non_lawyer_gates ───────────────────────────
        if config.get("non_lawyer_gates") and actor_role not in ("lawyer",):
            findings.append({
                "guardrail": "G3_non_lawyer_gates",
                "severity": "high",
                "message": f"Usuario con rol {actor_role!r} no puede firmar/enviar outputs legales sin supervision",
            })
            suggested_actions.append({
                "action": "require_lawyer_review",
                "reason": f"actor_role={actor_role}",
            })
            if destination in ("court", "regulator", "opposing_counsel"):
                block = True  # Bloquear envio externo si no es abogado

        # ─── G4 · attorney_contact append ─────────────────────
        contact_user_id = config.get("attorney_contact_user_id")
        if contact_user_id:
            contact_info = await self._fetch_user_contact(pool, contact_user_id)
            if contact_info:
                contact_block = (
                    f"\n\n---\n**Abogado responsable:** {contact_info.get('full_name') or contact_info['email']}  \n"
                    f"**Contacto:** {contact_info['email']}\n"
                )
                if contact_info["email"] not in draft_text:
                    suggested_actions.append({
                        "action": "append_attorney_contact",
                        "block": contact_block,
                    })

        # ─── G5 · escalation_rules ───────────────────────────
        escalation_triggered = []
        for rule in config.get("escalation_rules") or []:
            try:
                pattern = rule.get("trigger_regex")
                if not pattern:
                    continue
                if re.search(pattern, draft_text, flags=re.IGNORECASE):
                    escalation_triggered.append({
                        "rule_id": rule.get("id"),
                        "severity": rule.get("severity", "medium"),
                        "channel": rule.get("channel", "email"),
                        "message": rule.get("message", ""),
                    })
                    findings.append({
                        "guardrail": "G5_escalation_rule",
                        "severity": rule.get("severity", "medium"),
                        "rule_id": rule.get("id"),
                        "message": rule.get("message", ""),
                    })
            except re.error as e:
                logger.warning("apply_guardrails: regex invalida en rule %s: %s", rule.get("id"), e)

        if escalation_triggered:
            suggested_actions.append({
                "action": "notify_channels",
                "rules": escalation_triggered,
            })

        # ─── Persist audit a matter_history si aplica ─────────
        if matter_id and pool and ctx.firm_id:
            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        insert into matter_history
                            (matter_id, firm_id, event_type, actor_user_id,
                             actor_agent, summary, details)
                        values ($1::uuid, $2::uuid, 'guardrails_check', $3, 'lean_brain', $4, $5::jsonb)
                        """,
                        matter_id, str(ctx.firm_id),
                        str(ctx.user_id) if ctx.user_id else None,
                        f"Guardrails: {len(findings)} findings, block={block}",
                        json.dumps({
                            "findings": findings,
                            "destination": destination,
                            "actor_role": actor_role,
                            "block": block,
                        }, ensure_ascii=False, default=str),
                    )
            except Exception as e:
                logger.debug("apply_guardrails: history append fallo: %s", e)

        return {
            "block_output": block,
            "findings_count": len(findings),
            "findings": findings,
            "suggested_actions": suggested_actions,
            "destination": destination,
            "actor_role": actor_role,
            "config_source": config.get("_source", "firm_guardrails"),
        }

    # ─── Helpers ───────────────────────────────────────────────

    async def _load_config(self, pool, ctx) -> dict:
        """Lee firm_guardrails; si no existe, devuelve DEFAULT_GUARDRAILS."""
        if pool is None or ctx.firm_id is None:
            return {**DEFAULT_GUARDRAILS, "_source": "defaults_no_pool"}
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    select destination_check_enabled, work_product_header_text,
                           non_lawyer_gates, attorney_contact_user_id,
                           escalation_rules, notification_channels
                      from firm_guardrails
                     where firm_id = $1
                     limit 1
                    """,
                    str(ctx.firm_id),
                )
        except Exception as e:
            logger.warning("apply_guardrails: load_config fallo: %s", e)
            return {**DEFAULT_GUARDRAILS, "_source": "defaults_after_error"}

        if not row:
            return {**DEFAULT_GUARDRAILS, "_source": "defaults_no_row"}

        return {
            "destination_check_enabled": row["destination_check_enabled"],
            "work_product_header_text": row["work_product_header_text"] or DEFAULT_GUARDRAILS["work_product_header_text"],
            "non_lawyer_gates": row["non_lawyer_gates"],
            "attorney_contact_user_id": str(row["attorney_contact_user_id"]) if row["attorney_contact_user_id"] else None,
            "escalation_rules": list(row["escalation_rules"] or DEFAULT_GUARDRAILS["escalation_rules"]),
            "notification_channels": dict(row["notification_channels"] or {}),
            "_source": "firm_guardrails_table",
        }

    async def _fetch_user_contact(self, pool, user_id: str) -> Optional[dict]:
        if pool is None:
            return None
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "select id, email, full_name from users where id = $1 limit 1",
                    user_id,
                )
                if not row:
                    return None
                return {"id": str(row["id"]), "email": row["email"], "full_name": row["full_name"]}
        except Exception as e:
            logger.debug("apply_guardrails: fetch_user_contact fallo: %s", e)
            return None


def build_tool(pool=None, **_: Any) -> ToolDef:
    return ApplyGuardrailsTool(pool=pool)
