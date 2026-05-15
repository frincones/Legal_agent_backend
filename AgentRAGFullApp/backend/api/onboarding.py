"""Sprint 26 · Onboarding client API.

Endpoints (firm-scoped):
  GET   /v1/me/onboarding              · estado del checklist (snapshot vivo)
  POST  /v1/me/onboarding/skip         · skip un step manualmente
  POST  /v1/me/onboarding/seed-demo    · re-seed demo data (idempotente)
  DELETE /v1/me/onboarding/demo-data   · borra los datos demo

  GET   /v1/me/helper-tips             · tips contextuales para una ruta
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm
from utils.onboarding import get_state, mark_step, seed_demo_data, list_helper_tips

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/me", tags=["onboarding"])


@router.get("/onboarding")
async def my_onboarding(principal: Principal = Depends(get_current_firm)):
    return await get_state(principal.firm_id)


class SkipBody(BaseModel):
    step_key: str = Field(min_length=2, max_length=64)
    metadata: Optional[dict] = None


@router.post("/onboarding/skip")
async def skip_step(
    body: SkipBody,
    principal: Principal = Depends(get_current_firm),
):
    ok = await mark_step(principal.firm_id, body.step_key, "skipped", body.metadata)
    return {"ok": ok, "step": body.step_key, "status": "skipped"}


class CompleteBody(BaseModel):
    step_key: str = Field(min_length=2, max_length=64)
    metadata: Optional[dict] = None


@router.post("/onboarding/complete")
async def complete_step(
    body: CompleteBody,
    principal: Principal = Depends(get_current_firm),
):
    ok = await mark_step(principal.firm_id, body.step_key, "completed", body.metadata)
    return {"ok": ok, "step": body.step_key, "status": "completed"}


@router.post("/onboarding/seed-demo")
async def seed_demo(principal: Principal = Depends(get_current_firm)):
    """Re-seed demo data si no hay matters. Idempotente."""
    result = await seed_demo_data(principal.firm_id)
    return result


@router.delete("/onboarding/demo-data")
async def delete_demo_data(principal: Principal = Depends(get_current_firm)):
    """Borra clientes y matters marcados como is_demo=true en metadata."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        # First delete matters (cascade docs/timeline/notes)
        deleted_matters = await conn.fetchval(
            """
            delete from matters
             where firm_id = $1::uuid
               and coalesce((metadata->>'is_demo')::bool, false) = true
             returning (select count(*))
            """,
            principal.firm_id,
        )
        # Then clients (only those that have no real matters)
        deleted_clients = await conn.fetchval(
            """
            delete from clients
             where firm_id = $1::uuid
               and coalesce((metadata->>'is_demo')::bool, false) = true
               and not exists (select 1 from matters m where m.client_id = clients.id)
             returning (select count(*))
            """,
            principal.firm_id,
        )
    await mark_step(principal.firm_id, "demo_data_removed", "completed")
    return {"ok": True, "deleted_matters": deleted_matters or 0, "deleted_clients": deleted_clients or 0}


# ══════════════════════════════════════════════════════════════════════
# Helper tips (Lex Helper)
# ══════════════════════════════════════════════════════════════════════


@router.get("/helper-tips")
async def my_helper_tips(
    route: Optional[str] = None,
    module_key: Optional[str] = None,
    limit: int = 5,
    principal: Principal = Depends(get_current_firm),
):
    """Devuelve tips contextuales para la ruta o módulo actual."""
    tips = await list_helper_tips(route=route, module_key=module_key, limit=min(limit, 20))
    return {"items": tips, "route": route, "module_key": module_key}
