"""Sprint M21.S7 · CookbookExecutor.

Ejecuta un cookbook declarativo (steps en cookbook_registry.steps):
  - Para cada step:
    - Si tool: invoca el ToolDispatcher con la tool indicada
    - Si connector: invoca fetch_mcp_official con connector_id
    - Inputs se interpolan con {placeholder} desde inputs del cookbook
  - Persiste audit en cookbook_step_logs
  - Retorna outputs consolidados
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)


def _interpolate(template: Any, inputs: dict) -> Any:
    """Reemplaza {placeholder} en strings con valores de inputs (recursivo)."""
    if isinstance(template, str):
        out = template
        for k, v in inputs.items():
            out = out.replace("{" + k + "}", str(v) if v is not None else "")
        return out
    if isinstance(template, dict):
        return {k: _interpolate(v, inputs) for k, v in template.items()}
    if isinstance(template, list):
        return [_interpolate(x, inputs) for x in template]
    return template


async def execute_cookbook(
    *, cookbook_id: str, firm_id: UUID, user_id: Optional[UUID],
    inputs: dict, pool, anthropic_client=None, openai_client=None,
    matter_id: Optional[UUID] = None,
) -> dict:
    """Ejecuta el cookbook end-to-end. Persiste run + step logs."""
    if pool is None:
        return {"status": "error", "error_message": "pool unavailable"}

    # 1) Load cookbook spec
    async with pool.acquire() as conn:
        cb = await conn.fetchrow(
            "select cookbook_id, name, steps, inputs_schema from cookbook_registry where cookbook_id=$1",
            cookbook_id,
        )
    if cb is None:
        return {"status": "error", "error_message": f"cookbook {cookbook_id!r} no existe"}

    steps = list(cb["steps"] or [])
    if not steps:
        return {"status": "error", "error_message": "cookbook sin steps"}

    # 2) Create run
    run_id = uuid4()
    started = time.time()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            insert into cookbook_runs
                (run_id, firm_id, cookbook_id, matter_id, started_by_user_id, status, inputs)
            values ($1::uuid, $2::uuid, $3, $4, $5, 'running', $6::jsonb)
            """,
            str(run_id), str(firm_id), cookbook_id,
            str(matter_id) if matter_id else None,
            str(user_id) if user_id else None,
            json.dumps(inputs or {}, ensure_ascii=False, default=str),
        )

    # 3) Build ToolContext
    from lex.tools.base import ToolContext
    from lex.tools.registry import ToolRegistry

    tool_registry = ToolRegistry(pool=pool, anthropic_client=anthropic_client, openai_client=openai_client)

    ctx_base = ToolContext(
        generation_id=run_id, firm_id=firm_id, user_id=user_id,
        matter_id=matter_id, pool=pool,
        anthropic_client=anthropic_client, openai_client=openai_client,
        metadata={"cookbook_id": cookbook_id, "cookbook_run_id": str(run_id)},
    )

    # 4) Execute each step
    outputs: dict[str, Any] = {}
    overall_status = "ok"
    error_msg: Optional[str] = None

    for idx, step in enumerate(steps):
        step_name = step.get("name") or f"step_{idx}"
        step_started = time.time()
        step_status = "ok"
        step_output: Any = None
        step_err: Optional[str] = None
        try:
            step_inputs = _interpolate(step.get("inputs") or {}, inputs)
            tool_name = step.get("tool")
            connector_id = step.get("connector")

            if tool_name:
                tool = tool_registry.get(tool_name)
                if tool is None:
                    raise RuntimeError(f"tool {tool_name!r} no registrada")
                # Run tool
                step_output = await tool.run(ctx_base, **step_inputs)
            elif connector_id:
                # Use fetch_mcp_official tool with connector specified
                fetcher = tool_registry.get("fetch_mcp_official")
                if fetcher is None:
                    step_output = {"_skipped": True, "reason": "fetch_mcp_official tool no disponible"}
                else:
                    query = step.get("query")
                    if query:
                        query = _interpolate(query, inputs)
                    try:
                        step_output = await fetcher.run(
                            ctx_base, source=connector_id, query=query or "",
                        )
                    except TypeError:
                        # Schema diferente - degradar a stub
                        step_output = {"_warning": "fetch_mcp_official schema mismatch",
                                       "connector": connector_id, "query": query}
            else:
                step_output = {"_warning": "step sin tool ni connector"}

            outputs[step_name] = step_output
        except Exception as e:
            step_status = "error"
            step_err = f"{type(e).__name__}: {e}"
            overall_status = "error"
            if error_msg is None:
                error_msg = f"step {step_name!r}: {step_err}"
            logger.warning("cookbook step %s failed: %s", step_name, e)

        step_duration = int((time.time() - step_started) * 1000)
        # Persist step log
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    insert into cookbook_step_logs
                        (run_id, firm_id, step_index, step_name, finished_at,
                         duration_ms, status, output, error_message)
                    values ($1::uuid, $2::uuid, $3, $4, now(), $5, $6, $7::jsonb, $8)
                    """,
                    str(run_id), str(firm_id), idx, step_name,
                    step_duration, step_status,
                    json.dumps(step_output, default=str, ensure_ascii=False)[:50000] if step_output is not None else "{}",
                    step_err,
                )
        except Exception as e:
            logger.debug("step log insert failed: %s", e)

        if step_status == "error" and not step.get("continue_on_error"):
            # Break early
            break

    # 5) Finalize run
    duration_ms = int((time.time() - started) * 1000)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                update cookbook_runs
                   set finished_at = now(), duration_ms = $1, status = $2,
                       outputs = $3::jsonb, error_message = $4
                 where run_id = $5::uuid
                """,
                duration_ms, overall_status,
                json.dumps(outputs, default=str, ensure_ascii=False)[:200000],
                error_msg and error_msg[:1000],
                str(run_id),
            )
    except Exception as e:
        logger.warning("cookbook run finalize failed: %s", e)

    # Sprint M21.S8 · usage meter
    try:
        from lex.hardening.usage import record_usage
        await record_usage(pool, firm_id=firm_id, resource_type="cookbook_run", count=1)
    except Exception as e:
        logger.debug("cookbook executor: record_usage failed: %s", e)

    return {
        "run_id": str(run_id),
        "cookbook_id": cookbook_id,
        "status": overall_status,
        "duration_ms": duration_ms,
        "steps_executed": len(outputs),
        "outputs": outputs,
        "error_message": error_msg,
    }
