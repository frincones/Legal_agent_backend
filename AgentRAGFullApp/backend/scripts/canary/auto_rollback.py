"""Sprint M20.05 · S4.2 · Auto-rollback si error_rate excede threshold.

Diseñado para correr como cron job cada 5 min (Railway cron o GitHub Actions).
Si en la ventana de últimos N minutos error_rate > threshold:
  - Imprime alerta + mutation Railway sugerida para flip USE_LEAN_ORCHESTRATOR=false
  - Si --apply: ejecuta la mutation (requiere RAILWAY_API_TOKEN con permiso variableUpsert)
  - Si --slack-webhook: envía mensaje al canal

USO:
  python scripts/canary/auto_rollback.py --window-min 5 --threshold-pct 5
  python scripts/canary/auto_rollback.py --apply
  python scripts/canary/auto_rollback.py --slack-webhook https://hooks.slack.com/...

Exit codes:
  0 → todo bien (no rollback)
  1 → rollback emitido o sugerido
  2 → error al consultar
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.request
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


RAILWAY_PROJECT_ID = "2eb02ad0-a7ad-40e9-ba64-92a9a7a309fd"
RAILWAY_ENV_ID = "68f00e48-e699-4922-971c-e2adbde492c4"
RAILWAY_SERVICE_ID = "3d5eb807-196a-4cbb-98e7-51f83ed68d42"


async def compute_error_rate(window_min: int) -> dict:
    """Lee generation_audit + tool_call_audit y calcula error_rate."""
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        return {"_error": "DATABASE_URL no configurado"}

    import asyncpg
    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow(f"""
            select
              count(*) filter (where orchestrator_kind = 'lean') as lean_n,
              count(*) filter (where orchestrator_kind = 'lean' and validation_passed = false) as lean_fail,
              count(*) filter (where orchestrator_kind = 'legacy') as legacy_n,
              count(*) filter (where orchestrator_kind = 'legacy' and validation_passed = false) as legacy_fail
            from generation_audit
            where created_at > now() - interval '{int(window_min)} minutes'
        """)

        # Errores tool_call por arm
        tool_errors = {}
        try:
            rows = await conn.fetch(f"""
                select tool_name, count(*) filter (where success = false) as fail, count(*) as total
                from tool_call_audit
                where started_at > now() - interval '{int(window_min)} minutes'
                group by tool_name
                having count(*) filter (where success = false) > 0
                order by fail desc limit 10
            """)
            tool_errors = {r["tool_name"]: {"fail": r["fail"], "total": r["total"]} for r in rows}
        except Exception:
            pass
    finally:
        await conn.close()

    lean_n = row["lean_n"] or 0
    lean_fail = row["lean_fail"] or 0
    legacy_n = row["legacy_n"] or 0
    legacy_fail = row["legacy_fail"] or 0

    return {
        "window_min": window_min,
        "lean_n": lean_n,
        "lean_fail": lean_fail,
        "lean_error_rate_pct": (lean_fail / lean_n * 100) if lean_n else 0,
        "legacy_n": legacy_n,
        "legacy_fail": legacy_fail,
        "legacy_error_rate_pct": (legacy_fail / legacy_n * 100) if legacy_n else 0,
        "tool_errors_top": tool_errors,
    }


def notify_slack(webhook: str, msg: str) -> None:
    try:
        req = urllib.request.Request(
            webhook,
            data=json.dumps({"text": msg}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10).read()
        print("Slack notified.")
    except Exception as e:
        print(f"WARN slack failed: {e}", file=sys.stderr)


def emit_rollback_mutation(token: str, apply: bool) -> None:
    """Imprime (y opcionalmente ejecuta) la mutation para flip USE_LEAN=false."""
    mutation = f'''
mutation {{
  variableUpsert(input: {{
    projectId: "{RAILWAY_PROJECT_ID}"
    environmentId: "{RAILWAY_ENV_ID}"
    serviceId: "{RAILWAY_SERVICE_ID}"
    name: "USE_LEAN_ORCHESTRATOR"
    value: "false"
  }})
}}
'''
    print("\n=== ROLLBACK MUTATION ===")
    print(mutation)
    if not apply:
        print("[dry-run] no se aplica. Pasa --apply para ejecutar.")
        return

    print("\n[applying...]")
    req = urllib.request.Request(
        "https://backboard.railway.com/graphql/v2",
        data=json.dumps({"query": mutation}).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(r.read().decode("utf-8"))
    except Exception as e:
        print(f"ERROR aplicando mutation: {e}", file=sys.stderr)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--window-min", type=int, default=5)
    p.add_argument("--threshold-pct", type=float, default=5.0)
    p.add_argument("--min-samples", type=int, default=10)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--slack-webhook", default=os.getenv("SLACK_WEBHOOK_URL"))
    args = p.parse_args()

    metrics = asyncio.run(compute_error_rate(args.window_min))
    if metrics.get("_error"):
        print(f"ERROR: {metrics['_error']}", file=sys.stderr)
        return 2

    print(f"\n=== auto_rollback · ventana={args.window_min}min · threshold={args.threshold_pct}% ===")
    print(f"  lean:   {metrics['lean_n']:>4} runs · {metrics['lean_fail']} fail "
          f"· error_rate={metrics['lean_error_rate_pct']:.1f}%")
    print(f"  legacy: {metrics['legacy_n']:>4} runs · {metrics['legacy_fail']} fail "
          f"· error_rate={metrics['legacy_error_rate_pct']:.1f}%")

    if metrics["tool_errors_top"]:
        print(f"\n  Tool errors top:")
        for name, st in metrics["tool_errors_top"].items():
            print(f"    {name}: {st['fail']}/{st['total']}")

    # Decisión
    if metrics["lean_n"] < args.min_samples:
        print(f"\n[skip] muestras lean ({metrics['lean_n']}) < min_samples ({args.min_samples})")
        return 0
    if metrics["lean_error_rate_pct"] <= args.threshold_pct:
        print(f"\n✓ lean error_rate {metrics['lean_error_rate_pct']:.1f}% ≤ {args.threshold_pct}% · OK")
        return 0

    # ROLLBACK
    alert = (f"⚠️ AUTO-ROLLBACK LeanOrchestrator triggered\n"
             f"lean error_rate: {metrics['lean_error_rate_pct']:.1f}% (threshold {args.threshold_pct}%)\n"
             f"lean: {metrics['lean_n']} runs, {metrics['lean_fail']} fail\n"
             f"window: {args.window_min} min")
    print(f"\n✗ ROLLBACK NEEDED · {alert}")

    if args.slack_webhook:
        notify_slack(args.slack_webhook, alert)

    token = os.getenv("RAILWAY_API_TOKEN")
    if token:
        emit_rollback_mutation(token, args.apply)
    else:
        print("\nWARN: RAILWAY_API_TOKEN no configurado, no se puede emitir mutation automáticamente.")
        print("Hazlo manual en Railway UI: USE_LEAN_ORCHESTRATOR=false")

    return 1


if __name__ == "__main__":
    sys.exit(main())
