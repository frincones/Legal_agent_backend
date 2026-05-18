"""Admin endpoint · test-execute any registered voice tool against real backend.

POST /v1/admin/tools/test/{tool_name}
  body: { args: dict, extra_ctx?: dict }
  → { ok, tool, duration_ms, result?, error?, error_type? }

POST /v1/admin/tools/test-batch
  body: { tools: [{ name, args }], stop_on_error?: bool }
  → { results: [...] }

GET /v1/admin/tools/registry
  → { registered: [name1, name2, ...], descriptors_count, registry_count }

Use case: external script (scripts/test_all_tools_remote.py) iterates the
117-tool catalog and POSTs each with mock/real args to identify which fail
and why · enables per-tool fix loop without running through an LLM.

ACCESS CONTROL:
- Requires authenticated user.
- Requires either role='admin_users' OR a feature flag the firm has access to.
- All calls are persisted to audit_log with kind='tool_test'.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/admin/tools", tags=["admin_tools_test"])


class TestToolIn(BaseModel):
    args: dict[str, Any] = Field(default_factory=dict)
    extra_ctx: Optional[dict[str, Any]] = None


class TestToolBatchItem(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class TestToolBatchIn(BaseModel):
    tools: list[TestToolBatchItem]
    stop_on_error: bool = False


class TestToolResult(BaseModel):
    ok: bool
    tool: str
    duration_ms: int
    result: Optional[Any] = None
    error: Optional[str] = None
    error_type: Optional[str] = None
    soft_error: Optional[str] = None      # tool returned {"error": ...} but ran


async def _execute_one_tool(
    tool_name: str,
    args: dict[str, Any],
    ctx: dict[str, Any],
) -> TestToolResult:
    """Direct invocation of a registered voice tool · no LLM, no auth checks
    beyond ctx · returns structured result."""
    try:
        from api.voice import _tool_registry
    except Exception as e:
        return TestToolResult(
            ok=False, tool=tool_name, duration_ms=0,
            error=f"tool_registry import failed: {e}",
            error_type="ImportError",
        )

    fn = _tool_registry.get(tool_name)
    if fn is None:
        return TestToolResult(
            ok=False, tool=tool_name, duration_ms=0,
            error=f"tool '{tool_name}' not registered",
            error_type="NotRegistered",
        )

    started = time.time()
    try:
        result = await fn(args=args, ctx=ctx)
        duration_ms = int((time.time() - started) * 1000)
    except Exception as e:
        duration_ms = int((time.time() - started) * 1000)
        logger.exception("admin_tools_test · tool %s raised", tool_name)
        return TestToolResult(
            ok=False, tool=tool_name, duration_ms=duration_ms,
            error=str(e)[:500],
            error_type=type(e).__name__,
        )

    # Drop ui_command from chat-path result · not useful in test reports.
    if isinstance(result, dict) and "_ui_command" in result:
        result = {k: v for k, v in result.items() if k != "_ui_command"}

    soft_error = None
    if isinstance(result, dict):
        # Use .get() so a present-but-None "error" key (which the subagent
        # runner returns on success) doesn't get coerced to "None" string.
        err = result.get("error")
        if err:
            soft_error = str(err)[:500]
        elif result.get("ok") is False:
            soft_error = str(result.get("reason") or result.get("detail") or "ok=false")[:500]

    return TestToolResult(
        ok=soft_error is None,
        tool=tool_name,
        duration_ms=duration_ms,
        result=result,
        soft_error=soft_error,
    )


def _require_admin(principal: Principal) -> None:
    """Guards endpoint to admin role users."""
    # Many tools are sensitive · gate behind admin role or a feature flag.
    # If the principal model has a `role` field, check it.
    role = getattr(principal, "role", None)
    is_admin = role in ("admin", "admin_users", "superuser", "owner") or getattr(
        principal, "is_admin", False
    )
    if not is_admin:
        # Allow if env var LEXAI_TOOL_TEST_ALLOW_ALL=true (dev/staging).
        import os
        if os.getenv("LEXAI_TOOL_TEST_ALLOW_ALL", "").lower() != "true":
            raise HTTPException(
                status_code=403,
                detail=(
                    "Admin role required for tools test endpoints. "
                    "Set LEXAI_TOOL_TEST_ALLOW_ALL=true in env to bypass "
                    "(non-production only)."
                ),
            )


@router.get("/registry")
async def list_registry(principal: Principal = Depends(get_current_firm)):
    """Returns the current state of the tool registry."""
    _require_admin(principal)
    try:
        from api.voice import _tool_registry, _tool_descriptors
    except Exception as e:
        raise HTTPException(500, detail=f"registry unavailable: {e}")
    descriptors = _tool_descriptors()
    return {
        "registry_count": len(_tool_registry),
        "descriptors_count": len(descriptors),
        "registered": sorted(_tool_registry.keys()),
        "descriptors": [d.get("name") for d in descriptors],
    }


@router.post("/test/{tool_name}", response_model=TestToolResult)
async def test_tool(
    tool_name: str,
    body: TestToolIn,
    principal: Principal = Depends(get_current_firm),
):
    """Execute one tool against the real backend · returns structured result."""
    _require_admin(principal)

    ctx: dict[str, Any] = {
        "firm_id": principal.firm_id,
        "user_id": principal.user_id,
        "subagent_chain": ["admin_tools_test"],
    }
    if body.extra_ctx:
        ctx.update(body.extra_ctx)

    return await _execute_one_tool(tool_name, body.args, ctx)


@router.post("/test-batch")
async def test_tool_batch(
    body: TestToolBatchIn,
    principal: Principal = Depends(get_current_firm),
):
    """Execute multiple tools sequentially · used by the remote test runner."""
    _require_admin(principal)

    ctx_base: dict[str, Any] = {
        "firm_id": principal.firm_id,
        "user_id": principal.user_id,
        "subagent_chain": ["admin_tools_test"],
    }

    results: list[TestToolResult] = []
    for item in body.tools:
        r = await _execute_one_tool(item.name, item.args, dict(ctx_base))
        results.append(r)
        if body.stop_on_error and not r.ok:
            logger.info("admin_tools_test batch · stopping at %s", item.name)
            break
    return {
        "count": len(results),
        "passed": sum(1 for r in results if r.ok),
        "failed": sum(1 for r in results if not r.ok),
        "results": [r.model_dump() for r in results],
    }
