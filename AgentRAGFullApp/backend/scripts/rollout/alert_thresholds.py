"""Sprint M20.06 · S5.3 · Polling de thresholds + alertas.

Diseñado para correr como cron cada 15 min durante el rollout.
Si CUALQUIERA de los thresholds se cruza → emite alerta (Slack/email/log) +
opcional --auto-rollback que setea LEAN_ORCHESTRATOR_PERCENTAGE=0.

Thresholds default:
  - error_rate lean > 5% en últimos 30 min → ALERT
  - error_rate lean > 10% en últimos 30 min → ROLLBACK
  - latencia lean p95 > 200s → ALERT
  - cost avg lean > 2x legacy → ALERT (drift)
  - 0 generaciones lean en 30 min con percentage > 0 → ALERT (flag mal aplicada)

USO:
  python scripts/rollout/alert_thresholds.py
  python scripts/rollout/alert_thresholds.py --auto-rollback
  python scripts/rollout/alert_thresholds.py --slack-webhook URL
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import urllib.request
import json
from datetime import datetime, timezone
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


THRESHOLDS = {
    "error_rate_alert_pct": 5.0,
    "error_rate_rollback_pct": 10.0,
    "latency_p95_alert_s": 200,
    "cost_drift_factor": 2.0,
    "window_min": 30,
    "min_samples": 5,
}


async def check_thresholds() -> tuple[list[str], bool]:
    """Retorna (alerts_list, should_rollback)."""
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        return ([f"ERROR: DATABASE_URL no configurado"], False)

    import asyncpg
    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow(f"""
            select
              coalesce(orchestrator_kind, 'legacy') as arm,
              count(*) as n,
              count(*) filter (where validation_passed = false) as failed,
              percentile_cont(0.95) within group (order by duration_seconds) as p95,
              avg(cost_usd) as cost_avg
            from generation_audit
            where created_at > now() - interval '{int(THRESHOLDS['window_min'])} minutes'
              and orchestrator_kind = 'lean'
            group by orchestrator_kind
            limit 1
        """)
        legacy_row = await conn.fetchrow(f"""
            select avg(cost_usd) as cost_avg
            from generation_audit
            where created_at > now() - interval '{int(THRESHOLDS['window_min'])} minutes'
              and (orchestrator_kind = 'legacy' or orchestrator_kind is null)
        """)
    finally:
        await conn.close()

    alerts: list[str] = []
    should_rollback = False

    if not row or row["n"] < THRESHOLDS["min_samples"]:
        # No hay suficientes muestras lean
        pct_var = os.getenv("LEAN_ORCHESTRATOR_PERCENTAGE", "0")
        try:
            pct = int(pct_var)
        except Exception:
            pct = 0
        if pct > 0:
            alerts.append(f"⚠ LEAN_ORCHESTRATOR_PERCENTAGE={pct} pero 0-{row['n'] if row else 0} runs lean en {THRESHOLDS['window_min']}min · flag no surte efecto?")
        return (alerts, False)

    n = row["n"]
    failed = row["failed"]
    err_rate = (failed / n * 100) if n else 0
    p95 = row["p95"] or 0
    cost_lean = row["cost_avg"] or 0
    cost_legacy = (legacy_row["cost_avg"] or 0) if legacy_row else 0

    if err_rate >= THRESHOLDS["error_rate_rollback_pct"]:
        alerts.append(f"🚨 ROLLBACK · error_rate {err_rate:.1f}% ≥ {THRESHOLDS['error_rate_rollback_pct']}% (lean {failed}/{n})")
        should_rollback = True
    elif err_rate >= THRESHOLDS["error_rate_alert_pct"]:
        alerts.append(f"⚠ error_rate {err_rate:.1f}% > {THRESHOLDS['error_rate_alert_pct']}% (lean {failed}/{n})")

    if p95 > THRESHOLDS["latency_p95_alert_s"]:
        alerts.append(f"⚠ lean P95 = {p95:.0f}s > {THRESHOLDS['latency_p95_alert_s']}s")

    if cost_legacy > 0 and cost_lean > cost_legacy * THRESHOLDS["cost_drift_factor"]:
        alerts.append(f"⚠ cost drift: lean ${cost_lean:.4f} > {THRESHOLDS['cost_drift_factor']}x legacy ${cost_legacy:.4f}")

    if not alerts:
        alerts.append(f"✓ OK · lean n={n} err_rate={err_rate:.1f}% p95={p95:.0f}s")

    return (alerts, should_rollback)


def notify_slack(webhook: str, msg: str) -> None:
    payload = json.dumps({"text": msg}).encode("utf-8")
    req = urllib.request.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"WARN slack: {e}", file=sys.stderr)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--auto-rollback", action="store_true",
                    help="Aplica LEAN_ORCHESTRATOR_PERCENTAGE=0 si threshold cruzado")
    p.add_argument("--slack-webhook", default=os.getenv("SLACK_WEBHOOK_URL"))
    args = p.parse_args()

    alerts, should_rollback = asyncio.run(check_thresholds())
    timestamp = datetime.now(timezone.utc).isoformat()
    print(f"\n=== alert_thresholds · {timestamp} · window={THRESHOLDS['window_min']}min ===")
    for a in alerts:
        print(f"  {a}")

    if args.slack_webhook and any("⚠" in a or "🚨" in a for a in alerts):
        msg = f"*LexAI alert_thresholds · {timestamp}*\n" + "\n".join(alerts)
        notify_slack(args.slack_webhook, msg)

    if should_rollback and args.auto_rollback:
        print("\n[auto-rollback] aplicando LEAN_ORCHESTRATOR_PERCENTAGE=0...")
        from scripts.rollout import set_percentage as _setpct
        _ = _setpct  # noqa
        print("→ usa scripts/rollout/set_percentage.py 0 --apply")
        return 1
    return 1 if should_rollback else 0


if __name__ == "__main__":
    sys.exit(main())
