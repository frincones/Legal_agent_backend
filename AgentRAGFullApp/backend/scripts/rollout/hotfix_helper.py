"""Sprint M20.06 · S5.4 · Hotfix helper · iteración rápida sin redeploy completo.

Cuando se detecta calidad sospechosa en un edge case durante el rollout,
permite:
  1. Diagnosticar el fixture problemático (tools usadas, prompts enviados,
     respuestas Anthropic vía agent_traces)
  2. Probar ajuste local del system prompt sin tocar prod
  3. Generar PR mínimo con el fix (system prompt o tool description)

USO:
  python scripts/rollout/hotfix_helper.py diagnose <generation_id>
  python scripts/rollout/hotfix_helper.py test-prompt --intent "X" --doc-type Y
  python scripts/rollout/hotfix_helper.py suggest-pr --fixture-id poder_3_notarial_familia
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).parent.parent.parent


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(_BACKEND_ROOT / ".env")


async def cmd_diagnose(generation_id: str) -> int:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL"); return 1
    import asyncpg
    conn = await asyncpg.connect(dsn)
    try:
        # generation_audit
        ga = await conn.fetchrow(
            "select * from generation_audit where generation_id = $1::uuid",
            generation_id,
        )
        if not ga:
            print(f"No se encontró generation_id={generation_id}")
            return 1
        print(f"\n=== generation {generation_id} ===")
        print(f"  template:     {ga['template_id']}")
        print(f"  orchestrator: {ga.get('orchestrator_kind') or 'legacy'}")
        print(f"  duration:     {ga['duration_seconds']}s")
        print(f"  cost:         ${ga['cost_usd']}")
        print(f"  qa_score:     {ga['qa_score']}")
        print(f"  passed:       {ga['validation_passed']}")

        # tool_call_audit
        try:
            tools = await conn.fetch(
                "select tool_name, iteration, duration_ms, success, error_message, cached "
                "from tool_call_audit where generation_id = $1::uuid order by started_at",
                generation_id,
            )
            if tools:
                print(f"\n=== tool calls ({len(tools)}) ===")
                for t in tools:
                    icon = "✓" if t["success"] else "✗"
                    cache = " [cached]" if t["cached"] else ""
                    err = f" · {t['error_message']}" if t["error_message"] else ""
                    print(f"  iter={t['iteration']:>2} {icon} {t['tool_name']:<25} "
                          f"{t['duration_ms']:>5}ms{cache}{err}")
        except Exception as e:
            print(f"\n(tool_call_audit no disponible: {e})")

        # agent_traces
        try:
            traces = await conn.fetch(
                "select turn_number, llm_model, duration_ms, metadata "
                "from agent_traces where session_id = $1 order by turn_number",
                generation_id,
            )
            if traces:
                print(f"\n=== agent traces ({len(traces)}) ===")
                for tr in traces:
                    print(f"  turn={tr['turn_number']} model={tr['llm_model']} dur={tr['duration_ms']}ms")
        except Exception:
            pass
    finally:
        await conn.close()
    return 0


def cmd_test_prompt(intent: str, doc_type: str) -> int:
    """Corre localmente el LeanOrchestrator contra un intent para probar prompts."""
    print(f"\n=== Testing prompt local · doc_type={doc_type} ===")
    print(f"Intent: {intent}\n")
    print("Ejecuta esto en otra terminal:")
    print(f'  USE_LEAN_ORCHESTRATOR=true python -c "')
    print(f'import asyncio')
    print(f'from utils.db import get_storage')
    print(f'from utils.llm import get_openai_client')
    print(f'from utils.llm_provider import _get_anthropic_client')
    print(f'from lex.orchestrator.lean_orchestrator import LeanOrchestrator')
    print(f'from uuid import uuid4')
    print(f'async def run():')
    print(f'    storage = await get_storage()')
    print(f'    o = LeanOrchestrator(anthropic_client=_get_anthropic_client(),')
    print(f'        openai_client=get_openai_client(), pool=storage.pool,')
    print(f'        firm_id=uuid4(), user_id=uuid4(), generation_id=uuid4())')
    print(f'    async for ev in o.run(intent={intent!r}, doc_type_hint={doc_type!r}):')
    print(f'        print(ev.decode())')
    print(f'asyncio.run(run())')
    print(f'"')
    return 0


def cmd_suggest_pr(fixture_id: str) -> int:
    fixtures_path = _BACKEND_ROOT / "tests/fixtures/requests/parity_fixtures_v1.json"
    if not fixtures_path.exists():
        print(f"ERROR: fixtures no encontradas en {fixtures_path}")
        return 1
    data = json.loads(fixtures_path.read_text(encoding="utf-8"))
    fx = next((f for f in data["fixtures"] if f["id"] == fixture_id), None)
    if not fx:
        print(f"ERROR: fixture {fixture_id!r} no existe")
        return 1

    print(f"\n=== Fixture: {fixture_id} ===")
    print(f"  doc_type:    {fx['doc_type']}")
    print(f"  complexity:  {fx.get('complexity')}")
    print(f"  intent:      {fx['intent'][:200]}...")
    print()
    print(f"Pasos para hotfix:")
    print(f"  1. python scripts/rollout/hotfix_helper.py test-prompt \\")
    print(f"       --intent {fx['intent'][:80]!r} --doc-type {fx['doc_type']!r}")
    print(f"  2. Si el output no cumple expectativas, editar:")
    print(f"     · lex/brain/system_prompt.py (system prompt general)")
    print(f"     · lex/tools/{fx['doc_type'].split('_')[0]}*.py (description de tool específica)")
    print(f"  3. python -m tests.parity.runner_ab --fixture {fixture_id} --runs 3 --arm lean")
    print(f"  4. python -m tests.parity.llm_judge reports/parity_ab_*.json")
    print(f"  5. Si gates pasan: git commit + redeploy")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    p_diag = sub.add_parser("diagnose")
    p_diag.add_argument("generation_id")

    p_test = sub.add_parser("test-prompt")
    p_test.add_argument("--intent", required=True)
    p_test.add_argument("--doc-type", required=True)

    p_sugg = sub.add_parser("suggest-pr")
    p_sugg.add_argument("--fixture-id", required=True)

    args = p.parse_args()

    if args.cmd == "diagnose":
        return asyncio.run(cmd_diagnose(args.generation_id))
    if args.cmd == "test-prompt":
        return cmd_test_prompt(args.intent, args.doc_type)
    if args.cmd == "suggest-pr":
        return cmd_suggest_pr(args.fixture_id)
    return 1


if __name__ == "__main__":
    sys.exit(main())
