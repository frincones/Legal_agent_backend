"""Sprint 23 · Voice tools de billing · consulta plan + cuota desde voz.

Tools:
  · current_plan_status()           · estado actual + uso vs cuotas
  · remaining_quota(kind)           · cuánto queda de un kind específico
  · pricing_recommendation()        · sugerencia plan según uso reciente
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def current_plan_status_tool(args: dict, ctx: dict) -> dict:
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "no firm context"}
    from utils.quota_tracker import status
    s = await status(firm_id)
    plan = s.get("plan") or {}
    flags = s.get("flags") or {}
    usage = s.get("usage") or {}
    quotas = s.get("quotas") or {}
    near80 = [k for k in ("llm", "voice", "docs") if flags.get(f"near80_{k}") and not flags.get(f"over_{k}")]
    over = [k for k in ("llm", "voice", "docs") if flags.get(f"over_{k}")]
    return {
        "plan_code": plan.get("code"),
        "plan_name": plan.get("name"),
        "status": plan.get("status"),
        "trial_ends_at": plan.get("trial_ends_at"),
        "usage": usage,
        "quotas": quotas,
        "near80_kinds": near80,
        "over_kinds": over,
        "trial_expired": flags.get("trial_expired", False),
    }


KIND_MAP = {
    "llm": "llm_call",
    "voz": "voice_minute",
    "voice": "voice_minute",
    "documentos": "document_upload",
    "documents": "document_upload",
    "docs": "document_upload",
    "email": "email_sync",
    "judicial": "judicial_poll",
}


async def remaining_quota_tool(args: dict, ctx: dict) -> dict:
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "no firm context"}
    raw = (args.get("kind") or "llm").lower()
    kind = KIND_MAP.get(raw, raw)
    from utils.quota_tracker import precheck
    return await precheck(firm_id, kind, 0)


async def pricing_recommendation_tool(args: dict, ctx: dict) -> dict:
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "no firm context"}
    from utils.quota_tracker import status
    s = await status(firm_id)
    plan = (s.get("plan") or {}).get("code", "free")
    flags = s.get("flags") or {}
    if plan == "free" and any(flags.get(f"near80_{k}") for k in ("llm", "voice", "docs")):
        return {
            "current_plan": plan,
            "recommended": "pro",
            "reason": "Estás usando más del 80% de tu plan free. Pro te da 25× más capacidad por COP $149.000/mes.",
        }
    if plan == "pro" and any(flags.get(f"near80_{k}") for k in ("llm", "voice")):
        return {
            "current_plan": plan,
            "recommended": "firm",
            "reason": "Tu uso justifica el plan Firma con 5 usuarios + 4× más LLM.",
        }
    return {
        "current_plan": plan,
        "recommended": plan,
        "reason": "Tu plan actual cubre bien tu uso. No es necesario un upgrade.",
    }
