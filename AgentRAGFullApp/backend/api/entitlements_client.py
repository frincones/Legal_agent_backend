"""Sprint 25 · Entitlements client endpoint · expone snapshot al frontend.

Endpoints:
  GET /v1/me/entitlements        · snapshot completo (cacheable 30s)
  GET /v1/me/entitlements/check  · check rápido de un módulo específico
  GET /v1/me/entitlements/quota  · estado de una cuota específica
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends

from utils.auth import Principal, get_current_firm
from utils.entitlements import has_module, quota_for, entitlements, get_entitlement_mode

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/me/entitlements", tags=["entitlements"])


@router.get("")
async def my_entitlements(principal: Principal = Depends(get_current_firm)):
    """Snapshot completo · plan + modules + quotas. Cacheado 30s en backend.

    Frontend lo llama 1x al login + cada cambio de ruta crítico.
    Forma:
      {
        plan: { code, name, status, trial_ends_at },
        modules: { canvas: {enabled, category, name, has_override}, ... },
        quotas: { llm_calls: {limit, used, remaining, ...}, ... },
        snapshot_at: ISO
      }
    """
    snapshot = await entitlements(principal.firm_id)
    return {**snapshot, "enforcement_mode": get_entitlement_mode()}


@router.get("/check")
async def check_module(
    module: str,
    principal: Principal = Depends(get_current_firm),
):
    """Check rápido O(1) (cacheado 30s) · usado por componentes 'on demand'."""
    ok = await has_module(principal.firm_id, module)
    return {"module": module, "enabled": ok}


@router.get("/quota")
async def check_quota(
    kind: str,
    principal: Principal = Depends(get_current_firm),
):
    """Estado de una cuota · usado por QuotaBanner y componentes."""
    return await quota_for(principal.firm_id, kind)
