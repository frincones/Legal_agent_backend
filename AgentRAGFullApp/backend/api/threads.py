"""GET /v1/threads — recent assistant threads for SidebarHilosList.

Implementation: Option B — virtual grouping over skill_executions.
No new table is created. Each row in skill_executions represents one
agent interaction. We expose them ordered by started_at DESC so the
sidebar can show "today / yesterday / this week / before" groups.

The `title` is derived from:
  1. input_summary->>'command'  (the user's typed command e.g. "/revisar/contrato ...")
  2. input_summary->'context'->>'prompt'   (free-text prompt if available)
  3. Falls back to the skill command name (e.g. "/revisar/contrato")
  4. Ultimate fallback: "Hilo sin titulo"

The ID returned is the skill_execution UUID so the sidebar can link to
/threads/<id> (a future detail page can load the full execution record).

Auth: JWT with firm_id + user_id (multi-tenant).
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/threads", tags=["threads"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MAX_TITLE_WORDS = 8


def _make_title(command: Optional[str], input_summary: Optional[dict]) -> str:
    """Derive a human-readable title from the execution record.

    Priority:
      1. 'prompt' key in input_summary (free-text user instruction)
      2. 'command' key in input_summary (same as skill command sometimes)
      3. command column (skill path like "/revisar/contrato")
      4. "Hilo sin titulo"
    """
    prompt: Optional[str] = None
    if isinstance(input_summary, dict):
        prompt = (
            input_summary.get("prompt")
            or input_summary.get("command")
            or input_summary.get("message")
        )
        # Also try nested context
        if not prompt and isinstance(input_summary.get("context"), dict):
            prompt = input_summary["context"].get("prompt") or input_summary["context"].get("message")

    if prompt:
        words = str(prompt).strip().split()
        return " ".join(words[:_MAX_TITLE_WORDS]) + ("..." if len(words) > _MAX_TITLE_WORDS else "")

    if command:
        return str(command)

    return "Hilo sin titulo"


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get("")
async def list_threads(
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(get_current_firm),
):
    """Return recent skill executions as virtual threads for SidebarHilosList.

    Response shape (per-thread):
      id                 — skill_execution UUID (stable link target)
      title              — derived from input_summary or command
      created_at         — started_at of the execution (for date grouping)
      last_message_at    — same as created_at (single-record threads)
      message_count      — always 1 per execution row
      matter_id          — matter_id from execution, or null
    """
    from utils.db import get_storage

    storage = await get_storage()

    firm_id = principal.firm_id
    user_id = principal.user_id

    logger.info(
        "list_threads · firm_id=%s user_id=%s limit=%d pool_ok=%s",
        firm_id, user_id, limit,
        storage.pool is not None if storage else False,
    )

    sql = """
        select
            id,
            command,
            input_summary,
            matter_id,
            started_at
        from skill_executions
        where firm_id = $1::uuid
          and user_id = $2::uuid
          and started_at is not null
        order by started_at desc
        limit $3
    """

    rows = []
    _query_error: Optional[str] = None
    try:
        async with storage.pool.acquire() as conn:
            records = await conn.fetch(sql, firm_id, user_id, limit)
            for rec in records:
                import json as _json

                raw_summary = rec["input_summary"]
                if isinstance(raw_summary, str):
                    try:
                        raw_summary = _json.loads(raw_summary)
                    except Exception:
                        raw_summary = {}
                elif raw_summary is None:
                    raw_summary = {}

                title = _make_title(rec["command"], raw_summary)
                started = rec["started_at"]

                rows.append(
                    {
                        "id": str(rec["id"]),
                        "title": title,
                        "created_at": started.isoformat() if started else None,
                        "last_message_at": started.isoformat() if started else None,
                        "message_count": 1,
                        "matter_id": str(rec["matter_id"]) if rec["matter_id"] else None,
                    }
                )
    except Exception as exc:
        # Log con stack trace completo para diagnóstico (antes era sólo warning)
        logger.exception("list_threads query failed: %s", exc)
        _query_error = str(exc)
        # Retorna el error en el body para facilitar diagnóstico en dev/staging.
        # El sidebar frontend maneja lista vacía sin romperse.
        return {"threads": [], "total": 0, "_error": _query_error}

    return {"threads": rows, "total": len(rows)}
