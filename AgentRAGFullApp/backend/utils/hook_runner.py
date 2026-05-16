"""Sprint E · Hook runner · ejecuta hooks pre/post de skills."""

from __future__ import annotations

import importlib
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


async def run_hooks_for_skill(
    pool,
    command: str,
    hook_type: str,
    *,
    context: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Carga hooks activos para esta skill+tipo y los ejecuta secuencialmente.

    Retorna (decisions, hooks_fired_keys).
    decisions: lista de {hook_key, decision, reason, additional_context}
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "select * from lexai_get_active_hooks($1, $2)",
            command, hook_type,
        )

    decisions: list[dict[str, Any]] = []
    fired: list[str] = []
    for row in rows:
        try:
            mod_path = row["python_module"]
            func_name = row["function_name"] or "run"
            module = importlib.import_module(mod_path)
            fn = getattr(module, func_name, None)
            if not fn:
                logger.warning("hook %s missing function %s", mod_path, func_name)
                continue
            result = await fn(context, config=row.get("config") or {})
            fired.append(row["hook_key"])
            if result is None:
                continue
            # Result format: {"decision": "block"|"warn"|"approve", "reason": str, ...}
            mode = row.get("decision_mode") or "block"
            r_decision = result.get("decision", "approve")
            # Hook puede pedir bloquear; pero mode override:
            # · si decision_mode='log': cualquier resultado se loguea pero no bloquea
            # · si decision_mode='warn': block→warn, warn→warn, approve→approve
            # · si decision_mode='block': respeta el decision del hook
            if mode == "log":
                final_decision = "log"
            elif mode == "warn" and r_decision == "block":
                final_decision = "warn"
            else:
                final_decision = r_decision

            decisions.append({
                "hook_key": row["hook_key"],
                "decision": final_decision,
                "reason": result.get("reason"),
                "additional_context": result.get("additional_context"),
            })
        except Exception as e:
            logger.warning("hook %s failed (non-fatal): %s", row.get("hook_key"), e)
            fired.append(row.get("hook_key") + "_error")

    return decisions, fired
