"""Sprint 25 · Entitlements · async helpers + decorator.

Patrón:
  - has_module(firm_id, module_key)        → bool         (consulta lexai_has_module RPC)
  - quota_for(firm_id, quota_key)          → dict         (lexai_quota_for RPC)
  - entitlements(firm_id)                  → dict         (lexai_entitlements RPC, snapshot completo)
  - requires_module(module_key)            → decorator    (gate router con 403 si no tiene)

Diseño:
  - Cache LRU en-memoria por proceso (TTL 30s) para evitar hammer al DB en cada call.
  - LEXAI_ENTITLEMENT_MODE env var: 'strict' (default) | 'permissive' | 'off'
    * strict: si no tiene módulo → 403
    * permissive: si no tiene → log + permitir (fase de transición)
    * off: nunca evaluar (deshabilita el decorator)
"""

from __future__ import annotations

import functools
import json
import logging
import os
import time
from typing import Any, Optional
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)


def _ensure_dict(value: Any) -> Any:
    """asyncpg sin codec jsonb devuelve dict como str — parseamos a dict."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value

_MODE = os.getenv("LEXAI_ENTITLEMENT_MODE", "strict").lower()
_CACHE_TTL = int(os.getenv("LEXAI_ENTITLEMENT_CACHE_TTL", "30"))

# In-process cache: { (firm_id, key): (value, expires_at) }
_module_cache: dict[tuple[str, str], tuple[bool, float]] = {}
_quota_cache: dict[tuple[str, str], tuple[dict, float]] = {}
_entitlements_cache: dict[str, tuple[dict, float]] = {}


def invalidate_cache(firm_id: Optional[str] = None) -> None:
    """Invalida cache para una firm (o todo si firm_id=None). Llamar tras
    cambiar overrides o plan_modules."""
    if firm_id is None:
        _module_cache.clear()
        _quota_cache.clear()
        _entitlements_cache.clear()
        return
    fid = str(firm_id)
    for k in [k for k in _module_cache if k[0] == fid]:
        _module_cache.pop(k, None)
    for k in [k for k in _quota_cache if k[0] == fid]:
        _quota_cache.pop(k, None)
    _entitlements_cache.pop(fid, None)


async def has_module(firm_id: str | UUID, module_key: str) -> bool:
    """¿La firm tiene acceso al módulo? · Consulta RPC con cache 30s."""
    if _MODE == "off":
        return True
    fid = str(firm_id)
    cache_key = (fid, module_key)
    now = time.time()
    cached = _module_cache.get(cache_key)
    if cached and cached[1] > now:
        return cached[0]
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return True  # fail-open
    try:
        async with storage.pool.acquire() as conn:
            value = await conn.fetchval(
                "select lexai_has_module($1::uuid, $2)", fid, module_key,
            )
        value = bool(value)
        _module_cache[cache_key] = (value, now + _CACHE_TTL)
        return value
    except Exception as e:
        logger.warning("has_module error firm=%s module=%s: %s", fid, module_key, e)
        return True  # fail-open en caso de error


async def quota_for(firm_id: str | UUID, quota_key: str) -> dict[str, Any]:
    """Estado de una cuota · usado/limit/remaining/period."""
    fid = str(firm_id)
    cache_key = (fid, quota_key)
    now = time.time()
    cached = _quota_cache.get(cache_key)
    if cached and cached[1] > now:
        return cached[0]
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"limit": None, "used": 0, "remaining": None, "error": "no_storage"}
    try:
        async with storage.pool.acquire() as conn:
            value = await conn.fetchval(
                "select lexai_quota_for($1::uuid, $2)", fid, quota_key,
            )
        result = _ensure_dict(value) or {"limit": 0, "used": 0, "remaining": 0}
        _quota_cache[cache_key] = (result, now + _CACHE_TTL)
        return result
    except Exception as e:
        logger.warning("quota_for error firm=%s quota=%s: %s", fid, quota_key, e)
        return {"limit": None, "used": 0, "remaining": None, "error": str(e)[:80]}


async def entitlements(firm_id: str | UUID) -> dict[str, Any]:
    """Snapshot completo · plan + modules + quotas. Para frontend."""
    fid = str(firm_id)
    now = time.time()
    cached = _entitlements_cache.get(fid)
    if cached and cached[1] > now:
        return cached[0]
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"plan": {"code": "free"}, "modules": {}, "quotas": {}}
    async with storage.pool.acquire() as conn:
        result = await conn.fetchval("select lexai_entitlements($1::uuid)", fid)
    snapshot = _ensure_dict(result) or {"plan": {"code": "free"}, "modules": {}, "quotas": {}}
    _entitlements_cache[fid] = (snapshot, now + _CACHE_TTL)
    return snapshot


async def enforce_module(
    firm_id: str | UUID,
    module_key: str,
    request: Optional[Request] = None,
) -> None:
    """Verifica + lanza HTTPException(403) si no tiene módulo.
    Respeta LEXAI_ENTITLEMENT_MODE para permitir modo permisivo."""
    if _MODE == "off":
        return
    ok = await has_module(firm_id, module_key)
    if ok:
        return
    if _MODE == "permissive":
        logger.warning(
            "entitlement.permissive: firm=%s module=%s NOT entitled (allowed by mode)",
            firm_id, module_key,
        )
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error": "module_not_entitled",
            "module": module_key,
            "message": f"Tu plan actual no incluye este módulo ({module_key}). Actualiza tu plan para acceder.",
            "upgrade_url": "/settings/billing",
        },
    )


def requires_module(module_key: str):
    """FastAPI dependency factory · gate por módulo.

    Uso:
        from utils.entitlements import requires_module

        router = APIRouter(
            prefix="/v1/court-watcher",
            dependencies=[Depends(requires_module("judicial_polling"))],
        )

    O por endpoint específico:
        @router.get("/something", dependencies=[Depends(requires_module("canvas"))])
        async def endpoint(...): ...

    Modo de operación controlado por LEXAI_ENTITLEMENT_MODE:
      - strict (default): si no tiene → 403
      - permissive: si no tiene → log warning, permite
      - off: no evalúa
    """
    async def _dep(
        request: Request,
        principal: Principal = Depends(get_current_firm),
    ):
        await enforce_module(principal.firm_id, module_key, request=request)
        return True
    return _dep


def get_entitlement_mode() -> str:
    """Para debug / health endpoints."""
    return _MODE
