"""Sprint 23 · Quota tracker · increment + precheck + status helpers.

Patrón:
  - `precheck(firm_id, kind, amount)` → consulta lexai_check_quota
       devuelve dict {ok, plan, quota, used, remaining}
       NO incrementa nada. Llámalo antes de operaciones costosas.
  - `increment(firm_id, user_id, kind, count, cost, meta)` → llama
       lexai_increment_usage (inserta evento + actualiza counter).
       Idempotente a nivel de schema (no dedup; cada call cuenta).
  - `status(firm_id)` → consulta lexai_quota_status (snapshot completo).
  - `enforce(firm_id, kind, amount)` → precheck + raise HTTPException(402)
       si over quota. Helper para usar en routers.

Diseño:
  - Es opt-in: cualquier router/tool puede no llamar y todo sigue funcionando.
  - Si la firm no tiene firm_subscriptions row, lexai_check_quota devuelve
    plan='free' con cuotas del seed Sprint 6.
  - kind acepta: 'llm_call' | 'voice_minute' | 'document_upload' |
                 'email_sync' | 'judicial_poll' | 'canvas_generate'
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional
from uuid import UUID

from fastapi import HTTPException

logger = logging.getLogger(__name__)


def _ensure_dict(value: Any) -> Any:
    """asyncpg sin codec jsonb devuelve dict como str — parseamos a dict."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


async def precheck(
    firm_id: str | UUID,
    kind: str,
    amount: int = 1,
) -> dict[str, Any]:
    """Consulta cuota disponible. NO incrementa.

    Returns:
        dict con keys: ok, plan, quota, used, remaining, requested
        Si la firma no tiene plan: asume free.
        Si kind no es válido: ok=True, reason='unknown_kind'.
    """
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"ok": True, "reason": "no_storage"}
    try:
        async with storage.pool.acquire() as conn:
            result = await conn.fetchval(
                "select lexai_check_quota($1::uuid, $2, $3)",
                str(firm_id), kind, amount,
            )
        return _ensure_dict(result) or {"ok": True, "reason": "null_result"}
    except Exception as e:
        logger.warning("quota precheck error firm=%s kind=%s: %s", firm_id, kind, e)
        # Fail-open: nunca bloquear por error de cuota
        return {"ok": True, "reason": "error", "error": str(e)[:120]}


async def increment(
    firm_id: str | UUID,
    user_id: Optional[str | UUID],
    kind: str,
    count: int = 1,
    cost: float = 0.0,
    meta: Optional[dict[str, Any]] = None,
) -> bool:
    """Registra el uso (granular event + counter cache).

    Returns True si OK, False si hubo error. Nunca lanza excepción.
    """
    import json as _json
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return False
    try:
        async with storage.pool.acquire() as conn:
            await conn.execute(
                "select lexai_increment_usage($1::uuid, $2::uuid, $3, $4, $5, $6::jsonb)",
                str(firm_id),
                str(user_id) if user_id else None,
                kind,
                int(count),
                float(cost),
                _json.dumps(meta or {}),
            )
        return True
    except Exception as e:
        logger.warning("quota increment failed firm=%s kind=%s: %s", firm_id, kind, e)
        return False


async def status(firm_id: str | UUID) -> dict[str, Any]:
    """Snapshot completo de plan + cuotas + uso + flags."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"plan": {"code": "free"}, "quotas": {}, "usage": {}, "flags": {}}
    async with storage.pool.acquire() as conn:
        result = await conn.fetchval(
            "select lexai_quota_status($1::uuid)",
            str(firm_id),
        )
    return _ensure_dict(result) or {"plan": {"code": "free"}, "quotas": {}, "usage": {}, "flags": {}}


async def enforce(
    firm_id: str | UUID,
    kind: str,
    amount: int = 1,
) -> dict[str, Any]:
    """Precheck + raise HTTPException(402) si over quota.

    Usar en routers que necesitan bloquear duro la acción.
    Devuelve el dict de precheck si pasa.
    """
    info = await precheck(firm_id, kind, amount)
    if not info.get("ok", True):
        plan = info.get("plan", "free")
        used = info.get("used", 0)
        quota = info.get("quota", 0)
        raise HTTPException(
            status_code=402,
            detail={
                "error": "quota_exceeded",
                "kind": kind,
                "plan": plan,
                "used": used,
                "quota": quota,
                "requested": amount,
                "message": (
                    f"Has alcanzado el límite de tu plan ({plan}): "
                    f"{used}/{quota} {kind}. Actualiza tu plan para continuar."
                ),
                "upgrade_url": "/settings/billing",
            },
        )
    return info


async def has_feature(firm_id: str | UUID, feature: str) -> bool:
    """Comprueba si la firma tiene una feature flag activa según su plan.

    feature: 'court_watcher' | 'email_ingest' | 'voice' | 'canvas' |
             'calc' | 'briefing' | 'priority_support'
    """
    s = await status(firm_id)
    features = s.get("features") or {}
    return bool(features.get(feature, False))
