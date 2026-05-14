"""Sprint 17 · My Day API.

Endpoint único que agrega TODO lo que un abogado necesita ver al iniciar
su día: tareas asignadas, plazos próximos, menciones sin leer, comentarios
donde lo mencionaron y predicciones recientes.

Usa el RPC `lexai_my_day` (Sprint 17 migration) que devuelve un JSONB con
todos los buckets en una sola consulta.

  GET /v1/my-day?horizon_days=7
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/my-day", tags=["my_day"])


@router.get("")
async def my_day(
    horizon_days: int = Query(default=7, ge=1, le=60),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {}
    async with storage.pool.acquire() as conn:
        result = await conn.fetchval(
            "select lexai_my_day($1::uuid, $2::uuid, $3)",
            principal.firm_id, principal.user_id, horizon_days,
        )
    if result is None:
        return {}
    if isinstance(result, str):
        try:
            return json.loads(result)
        except Exception:
            return {}
    return result
