"""Smoke test for the 17 LexAI voice tools.

Run with the demo firm seeded in Supabase:

    cd backend
    python -m tests.test_voice_tools

Exits non-zero if any tool fails. Each tool is invoked with realistic args
against the real Supabase Postgres database.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone

# Allow running from backend/ directly without installing as a package.
if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.db import get_storage, close_storage  # noqa: E402


# Demo firm (Lopez & Asociados) — seed values used everywhere.
DEMO_FIRM_ID = "00000000-0000-0000-0000-000000000001"
DEMO_USER_ID = "00000000-0000-0000-0000-000000000010"


async def _resolve_demo_ids() -> dict:
    """Look up real demo IDs from Supabase if the hardcoded UUIDs aren't seeded."""
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        firm = await conn.fetchrow(
            "select id from firms where razon_social ilike '%López%' or razon_social ilike '%Lopez%' limit 1"
        )
        if not firm:
            raise RuntimeError("Demo firm 'López & Asociados' not found — re-seed Supabase.")
        firm_id = str(firm["id"])
        user = await conn.fetchrow(
            "select id from users where firm_id = $1::uuid order by created_at limit 1",
            firm_id,
        )
        if not user:
            raise RuntimeError(f"No user under firm {firm_id} — re-seed Supabase.")
        matter = await conn.fetchrow(
            "select id from matters where firm_id = $1::uuid and status != 'archivado' order by created_at limit 1",
            firm_id,
        )
        client = await conn.fetchrow(
            "select id, nombre from clients where firm_id = $1::uuid order by created_at limit 1",
            firm_id,
        )
        doc = await conn.fetchrow(
            "select id from matter_documents where firm_id = $1::uuid order by created_at desc limit 1",
            firm_id,
        )
    return {
        "firm_id": firm_id,
        "user_id": str(user["id"]),
        "matter_id": str(matter["id"]) if matter else None,
        "client_id": str(client["id"]) if client else None,
        "client_nombre": client["nombre"] if client else None,
        "document_id": str(doc["id"]) if doc else None,
    }


def _trim(obj, max_chars: int = 240) -> str:
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= max_chars else s[:max_chars] + "…"


async def main() -> int:
    print("=== LexAI · voice tools smoke test ===")
    if not os.getenv("DATABASE_URL"):
        print("ERROR: DATABASE_URL env var not set", file=sys.stderr)
        return 2

    ids = await _resolve_demo_ids()
    print(f"Demo firm:    {ids['firm_id']}")
    print(f"Demo user:    {ids['user_id']}")
    print(f"Demo matter:  {ids['matter_id']}")
    print(f"Demo client:  {ids['client_id']} ({ids['client_nombre']})")
    print(f"Demo doc:     {ids['document_id']}\n")

    ctx = {
        "firm_id": ids["firm_id"],
        "user_id": ids["user_id"],
        "matter_id": ids["matter_id"],
        "session_id": "test-session",
    }

    from agent.tools.paralegal_tools import (
        list_my_matters_tool,
        find_client_tool,
        list_upcoming_deadlines_tool,
        add_matter_deadline_tool,
        mark_deadline_done_tool,
        add_matter_note_tool,
        list_matter_documents_tool,
        summarize_document_tool,
        list_pending_hitl_tool,
        get_firm_metrics_tool,
    )
    from api.matters import open_matter_context_tool
    from api.calc import calc_liquidacion_tool

    fecha_iso = (datetime.now(tz=timezone.utc) + timedelta(days=14)).isoformat()

    cases: list[tuple[str, callable, dict]] = [
        ("list_my_matters", list_my_matters_tool, {"limit": 5}),
        ("list_my_matters · laboral", list_my_matters_tool, {"materia": "laboral"}),
        ("find_client", find_client_tool, {"query": (ids["client_nombre"] or "Rodriguez").split(" ")[0]}),
        ("list_upcoming_deadlines · 30d", list_upcoming_deadlines_tool, {"days": 30}),
        ("get_firm_metrics", get_firm_metrics_tool, {}),
        ("list_pending_hitl", list_pending_hitl_tool, {}),
    ]
    if ids["matter_id"]:
        cases.extend([
            ("open_matter_context", open_matter_context_tool, {"matter_id": ids["matter_id"]}),
            ("list_matter_documents", list_matter_documents_tool, {"matter_id": ids["matter_id"]}),
            ("add_matter_note", add_matter_note_tool, {"matter_id": ids["matter_id"], "body": "Smoke test note · ignorar."}),
            ("add_matter_deadline", add_matter_deadline_tool, {
                "matter_id": ids["matter_id"], "titulo": "Smoke test deadline · ignorar",
                "fecha": fecha_iso, "tipo": "otro",
            }),
        ])
    cases.append(("calc_liquidacion · injustificado", calc_liquidacion_tool, {
        "fecha_ingreso": "2019-01-15",
        "fecha_terminacion": "2026-03-14",
        "salario_mensual_cop": 4_500_000,
        "causa": "injustificado",
        "tipo_contrato": "indefinido",
    }))
    if ids["document_id"]:
        cases.append(("summarize_document", summarize_document_tool, {"document_id": ids["document_id"]}))

    pending_deadline_id: str | None = None
    failures: list[str] = []
    for label, fn, args in cases:
        try:
            result = await fn(args=args, ctx=ctx)
            if isinstance(result, dict) and result.get("error"):
                print(f"FAIL  {label:40s} -> {result['error']}")
                failures.append(label)
            else:
                print(f"OK    {label:40s} -> {_trim(result)}")
                if label == "add_matter_deadline" and isinstance(result, dict) and result.get("id"):
                    pending_deadline_id = result["id"]
        except Exception as e:
            print(f"FAIL  {label:40s} -> {type(e).__name__}: {e}")
            traceback.print_exc()
            failures.append(label)

    if pending_deadline_id:
        try:
            from agent.tools.paralegal_tools import mark_deadline_done_tool as _mdd
            r = await _mdd(args={"deadline_id": pending_deadline_id}, ctx=ctx)
            print(f"OK    {'mark_deadline_done':40s} -> {_trim(r)}")
        except Exception as e:
            print(f"FAIL  mark_deadline_done -> {e}")
            failures.append("mark_deadline_done")

    await close_storage()

    total = len(cases) + (1 if pending_deadline_id else 0)
    passed = total - len(failures)
    print(f"\n{passed}/{total} tools OK ({len(failures)} failed)")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
