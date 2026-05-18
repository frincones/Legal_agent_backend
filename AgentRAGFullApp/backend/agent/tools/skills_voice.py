"""Sprint E · Voice tools para invocar skills + manejar redlines."""

from __future__ import annotations

import logging
from typing import Any

from utils.skill_runner import run_skill

logger = logging.getLogger(__name__)


async def execute_skill_tool(args: dict, ctx: dict) -> dict:
    """Voice tool · ejecuta una skill por command desde el voice agent."""
    command = args.get("command")
    if not command or not command.startswith("/"):
        return {"ok": False, "error": "command required (e.g. /revisar/contrato)"}

    from utils.db import get_storage
    storage = await get_storage()

    result = await run_skill(
        storage.pool,
        firm_id=ctx.get("firm_id"),
        user_id=ctx.get("user_id"),
        command=command,
        input_data={
            "prompt": args.get("prompt") or "",
            "matter_id": ctx.get("matter_id"),
            "matter_titulo": args.get("matter_titulo"),
            "args": args.get("args") or {},
        },
        matter_id=ctx.get("matter_id"),
    )
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error"),
                "reason": result.get("reason") or result.get("detail")}
    output = result.get("output") or {}
    return {
        "ok": True,
        "command": command,
        "summary": output.get("summary") or output.get("text", "")[:600],
        "warnings": result.get("warnings", []),
    }


async def review_contract_tool(args: dict, ctx: dict) -> dict:
    """Voice tool · invoca /revisar/contrato sobre el doc activo o uno por id."""
    document_id = args.get("document_id") or ctx.get("active_document_id")
    if not document_id:
        return {"ok": False, "error": "necesito document_id del contrato"}

    from utils.db import get_storage
    storage = await get_storage()

    # Cargar contenido del documento desde matter_documents
    async with storage.pool.acquire() as conn:
        doc = await conn.fetchrow(
            "select id, titulo, mime_type, storage_path, resumen_ia from matter_documents "
            "where id = $1::uuid and firm_id = $2::uuid",
            document_id, ctx.get("firm_id"),
        )
    if not doc:
        return {"ok": False, "error": "documento no encontrado"}

    result = await run_skill(
        storage.pool,
        firm_id=ctx.get("firm_id"),
        user_id=ctx.get("user_id"),
        command="/revisar/contrato",
        input_data={
            "matter_titulo": args.get("matter_titulo"),
            "document_text": doc.get("resumen_ia") or doc.get("titulo") or "",
            "matter_id": ctx.get("matter_id"),
            "prompt": "Revisa cláusula por cláusula. Devuelve clauses con severity green/yellow/red.",
        },
        matter_id=ctx.get("matter_id"),
        document_id=document_id,
    )
    if not result.get("ok"):
        return result
    output = result.get("output") or {}
    clauses = output.get("clauses", [])
    summary = output.get("severity_summary", {})
    return {
        "ok": True,
        "clause_count": len(clauses),
        "severity_summary": summary,
        "warnings": result.get("warnings", []),
        "summary_text": (
            f"Revisión completada · {summary.get('red', 0)} cláusulas RED, "
            f"{summary.get('yellow', 0)} YELLOW, {summary.get('green', 0)} GREEN"
        ),
    }


async def apply_redline_tool(args: dict, ctx: dict) -> dict:
    """Voice tool · acepta redlines por ID o todos."""
    redline_set_id = args.get("redline_set_id")
    if not redline_set_id:
        return {"ok": False, "error": "redline_set_id required"}
    accept_ids = args.get("accept_ids") or []

    from utils.db import get_storage
    from utils.redline_diff import apply_redlines
    import json
    storage = await get_storage()

    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            "select redlines, original_text from canvas_redlines "
            "where id = $1::uuid and firm_id = $2::uuid",
            redline_set_id, ctx.get("firm_id"),
        )
    if not row:
        return {"ok": False, "error": "redline set no encontrado"}

    redlines = row["redlines"]
    if isinstance(redlines, str):
        redlines = json.loads(redlines)
    if not accept_ids:
        accept_ids = [r.get("id") for r in redlines if r.get("id")]
    accept_set = set(accept_ids)

    result_text = apply_redlines(row["original_text"], redlines, only_ids=accept_set)

    async with storage.pool.acquire() as conn:
        await conn.execute(
            """update canvas_redlines
                  set status = 'applied', applied_count = $1,
                      result_text = $2, applied_by = $3::uuid, applied_at = now()
                where id = $4::uuid""",
            len(accept_set), result_text, ctx.get("user_id"), redline_set_id,
        )

    from agent.tools._ui_events import ui_data_changed
    return {
        "ok": True,
        "applied_count": len(accept_set),
        "summary": f"Aplicados {len(accept_set)} de {len(redlines)} redlines",
        "_ui_command": ui_data_changed(
            "redlines", matter_id=ctx.get("matter_id"), firm_id=ctx.get("firm_id"),
            op="update", extra={"redline_set_id": redline_set_id, "status": "applied"},
        ),
    }


async def reject_redline_tool(args: dict, ctx: dict) -> dict:
    """Voice tool · rechaza todos los redlines de un set."""
    redline_set_id = args.get("redline_set_id")
    if not redline_set_id:
        return {"ok": False, "error": "redline_set_id required"}

    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        r = await conn.fetchrow(
            """update canvas_redlines
                  set status = 'rejected', result_text = original_text,
                      applied_by = $1::uuid, applied_at = now()
                where id = $2::uuid and firm_id = $3::uuid returning id""",
            ctx.get("user_id"), redline_set_id, ctx.get("firm_id"),
        )
    if not r:
        return {"ok": False, "error": "redline set no encontrado"}
    from agent.tools._ui_events import ui_data_changed
    return {
        "ok": True,
        "summary": "Todos los redlines rechazados · texto original preservado",
        "_ui_command": ui_data_changed(
            "redlines", matter_id=ctx.get("matter_id"), firm_id=ctx.get("firm_id"),
            op="update", extra={"redline_set_id": redline_set_id, "status": "rejected"},
        ),
    }
