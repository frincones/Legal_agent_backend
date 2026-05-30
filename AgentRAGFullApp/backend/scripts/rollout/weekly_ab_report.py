"""Sprint M20.06 · S5.2 · Reporte semanal A/B automatizado.

Diseñado para correr cron semanal (lunes 09:00). Genera reporte ejecutivo:
  - Comparativa lean vs legacy en últimos 7 días
  - Top 5 doc_types por volumen
  - Top 3 tools más usadas (lean only)
  - Errores recurrentes
  - Recomendación: avanzar rollout / pausar / rollback

Output:
  - reports/weekly_ab_YYYY_W##.md
  - opcionalmente → Slack canal #lexai-rollout

USO:
  python scripts/rollout/weekly_ab_report.py
  python scripts/rollout/weekly_ab_report.py --slack-webhook URL
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.request
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


def recommend(metrics: dict) -> tuple[str, list[str]]:
    """Recomienda próximo paso basado en métricas."""
    reasons = []
    arms = {r["arm"]: r for r in metrics["by_arm"]}
    leg = arms.get("legacy")
    ln = arms.get("lean")

    if not ln or ln["n"] < 50:
        return ("INSUFFICIENT_DATA",
                [f"Solo {ln['n'] if ln else 0} runs lean en la ventana. Necesita ≥ 50 para decidir."])

    pass_pct_lean = (ln["passed"] / ln["n"] * 100) if ln["n"] else 0
    if pass_pct_lean < 80:
        reasons.append(f"⚠ lean validation pass = {pass_pct_lean:.0f}% < 80%")
        return ("HOLD", reasons)

    if leg and leg["n"] >= 20:
        # Comparativas
        lat_l = leg["p50"] or 0
        lat_le = ln["p50"] or 0
        if lat_le > lat_l * 1.5:
            reasons.append(f"⚠ lean p50 = {lat_le:.1f}s > 1.5x legacy ({lat_l:.1f}s)")
            return ("HOLD", reasons)
        cost_l = leg["cost_avg"] or 0
        cost_le = ln["cost_avg"] or 0
        if cost_l and cost_le > cost_l * 1.2:
            reasons.append(f"⚠ lean cost = ${cost_le:.4f} > 1.2x legacy (${cost_l:.4f})")
            return ("HOLD", reasons)

    reasons.append(f"✓ lean pass={pass_pct_lean:.0f}%, n={ln['n']}")
    if leg and ln:
        d_lat = ((ln["p50"] or 0) - (leg["p50"] or 0)) / (leg["p50"] or 1) * 100
        d_cost = ((ln["cost_avg"] or 0) - (leg["cost_avg"] or 0)) / (leg["cost_avg"] or 1) * 100
        reasons.append(f"✓ Δ latencia: {d_lat:+.1f}%")
        reasons.append(f"✓ Δ costo: {d_cost:+.1f}%")
    return ("ADVANCE", reasons)


def render_report(metrics: dict, recommendation: tuple[str, list[str]]) -> str:
    week = datetime.now(timezone.utc).strftime("%Y-W%V")
    rec_action, rec_reasons = recommendation
    lines = [
        f"# LexAI · Weekly A/B Report · {week}",
        f"",
        f"**Ventana:** {metrics['window_hours']}h ({metrics['window_hours']//24}d)",
        f"**Capturado:** {metrics['captured_at']}",
        f"",
        f"## Recomendación: **{rec_action}**",
        f"",
    ]
    for r in rec_reasons:
        lines.append(f"- {r}")

    lines += ["", "## Resumen por arm", ""]
    arms = {r["arm"]: r for r in metrics["by_arm"]}
    for arm in ("legacy", "lean"):
        r = arms.get(arm)
        if not r:
            lines.append(f"### {arm}: sin datos")
            continue
        lines.append(f"### {arm}")
        lines.append(f"- N: {r['n']}")
        lines.append(f"- P50: {r['p50'] or 0:.1f}s · P95: {r['p95'] or 0:.1f}s")
        lines.append(f"- Cost avg: ${r['cost_avg'] or 0:.4f} · total: ${r['cost_total'] or 0:.2f}")
        lines.append(f"- QA avg: {r['qa_avg'] or 0:.2f}")
        lines.append(f"- Pass: {r['passed']} · Fail: {r['failed']}")
        lines.append(f"- Distinct firms: {r['distinct_firms']}")
        if arm == "lean":
            lines.append(f"- Cache tokens: {(r['cache_tokens_sum'] or 0)/1000:.1f}K")
        lines.append("")

    return "\n".join(lines)


async def main_async() -> tuple[str, dict, tuple]:
    from .ab_metrics_compare import fetch_ab_metrics
    metrics = await fetch_ab_metrics(168)   # 7 días
    if metrics.get("_error"):
        return "", metrics, ("ERROR", [metrics["_error"]])
    recommendation = recommend(metrics)
    report = render_report(metrics, recommendation)
    return report, metrics, recommendation


def notify_slack(webhook: str, report: str) -> None:
    payload = json.dumps({"text": report[:3500]}).encode("utf-8")
    req = urllib.request.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
        print("Slack notified.")
    except Exception as e:
        print(f"WARN slack failed: {e}", file=sys.stderr)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--slack-webhook", default=os.getenv("SLACK_WEBHOOK_URL"))
    p.add_argument("--output", type=Path, default=None)
    args = p.parse_args()

    report, metrics, _ = asyncio.run(main_async())
    print(report)

    if not args.output:
        week = datetime.now(timezone.utc).strftime("%Y_W%V")
        args.output = _BACKEND_ROOT / "reports" / f"weekly_ab_{week}.md"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    args.output.with_suffix(".json").write_text(
        json.dumps(metrics, indent=2, default=str, ensure_ascii=False), encoding="utf-8",
    )
    print(f"\n→ guardado: {args.output}")

    if args.slack_webhook:
        notify_slack(args.slack_webhook, report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
