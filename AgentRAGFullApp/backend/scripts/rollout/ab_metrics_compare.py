"""Sprint M20.06 · S5.2 · Comparación rigurosa A/B legacy vs lean en prod.

Query agregada por arm sobre `generation_audit` + `tool_call_audit` con
ventana configurable (horas o días). Calcula:
  - latencia p50/p95
  - costo avg y total
  - quality_score avg
  - error_rate
  - cache_hit_rate (para lean)
  - distribución por doc_type

Genera output JSON + tabla MD legible.

USO:
  python scripts/rollout/ab_metrics_compare.py                # últimas 24h
  python scripts/rollout/ab_metrics_compare.py --hours 168    # última semana
  python scripts/rollout/ab_metrics_compare.py --output reports/ab_week_2026_W22.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


async def fetch_ab_metrics(hours: int) -> dict:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        return {"_error": "DATABASE_URL no configurado"}

    import asyncpg
    conn = await asyncpg.connect(dsn)
    try:
        # Métricas globales por arm
        by_arm = await conn.fetch(f"""
            select
              coalesce(orchestrator_kind, 'legacy') as arm,
              count(*) as n,
              percentile_cont(0.5) within group (order by duration_seconds) as p50,
              percentile_cont(0.95) within group (order by duration_seconds) as p95,
              percentile_cont(0.99) within group (order by duration_seconds) as p99,
              avg(cost_usd) as cost_avg,
              sum(cost_usd) as cost_total,
              avg(qa_score) as qa_avg,
              sum(cache_hit_tokens) as cache_tokens_sum,
              count(*) filter (where validation_passed = true) as passed,
              count(*) filter (where validation_passed = false) as failed,
              count(distinct firm_id) as distinct_firms
            from generation_audit
            where created_at > now() - interval '{int(hours)} hours'
            group by orchestrator_kind
        """)

        # Por doc_type
        by_template = await conn.fetch(f"""
            select
              template_id,
              coalesce(orchestrator_kind, 'legacy') as arm,
              count(*) as n,
              avg(duration_seconds) as lat_avg,
              avg(cost_usd) as cost_avg,
              avg(qa_score) as qa_avg
            from generation_audit
            where created_at > now() - interval '{int(hours)} hours'
            group by template_id, orchestrator_kind
            order by template_id, orchestrator_kind
        """)

        # Tool stats para lean
        try:
            tool_stats = await conn.fetch(f"""
                select
                  tool_name,
                  count(*) as n,
                  avg(duration_ms) as avg_ms,
                  count(*) filter (where success = true) as ok,
                  count(*) filter (where success = false) as fail,
                  count(*) filter (where cached = true) as cached
                from tool_call_audit
                where started_at > now() - interval '{int(hours)} hours'
                group by tool_name
                order by n desc
            """)
        except Exception:
            tool_stats = []
    finally:
        await conn.close()

    return {
        "window_hours": hours,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "by_arm": [dict(r) for r in by_arm],
        "by_template": [dict(r) for r in by_template],
        "tool_stats": [dict(r) for r in tool_stats],
    }


def render_md(metrics: dict) -> str:
    lines = [f"# A/B Metrics Compare · {metrics['window_hours']}h window", "",
             f"_Captured at: {metrics['captured_at']}_", "",
             "## Por arm", "",
             "| Arm | N | P50 (s) | P95 (s) | P99 (s) | Cost avg | Cost total | QA avg | Pass% | Cache K |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    arms = {r["arm"]: r for r in metrics["by_arm"]}
    for arm in ("legacy", "lean"):
        r = arms.get(arm)
        if not r:
            lines.append(f"| {arm} | 0 | – | – | – | – | – | – | – | – |")
            continue
        n = r["n"]
        pass_pct = (r["passed"] / n * 100) if n else 0
        cache_k = (r["cache_tokens_sum"] or 0) / 1000
        lines.append(
            f"| {arm} | {n} | {r['p50'] or 0:.1f} | {r['p95'] or 0:.1f} | {r['p99'] or 0:.1f} | "
            f"${r['cost_avg'] or 0:.4f} | ${r['cost_total'] or 0:.2f} | "
            f"{r['qa_avg'] or 0:.2f} | {pass_pct:.0f}% | {cache_k:.1f}K |"
        )

    # Comparativa delta
    if "legacy" in arms and "lean" in arms:
        leg = arms["legacy"]
        ln = arms["lean"]
        d_lat = ((ln["p50"] or 0) - (leg["p50"] or 0)) / (leg["p50"] or 1) * 100
        d_cost = ((ln["cost_avg"] or 0) - (leg["cost_avg"] or 0)) / (leg["cost_avg"] or 1) * 100
        d_qa = (ln["qa_avg"] or 0) - (leg["qa_avg"] or 0)
        lines += ["",
                  "## Delta lean vs legacy",
                  "",
                  f"- **Latencia P50:** {d_lat:+.1f}%",
                  f"- **Costo avg:** {d_cost:+.1f}%",
                  f"- **QA score:** {d_qa:+.2f}",
                  ""]

    # Por template
    lines += ["## Por template", "",
              "| Template | Arm | N | Lat avg | Cost avg | QA avg |",
              "|---|---|---|---|---|---|"]
    for r in metrics["by_template"]:
        lines.append(
            f"| {r['template_id']} | {r['arm']} | {r['n']} | "
            f"{r['lat_avg'] or 0:.1f}s | ${r['cost_avg'] or 0:.4f} | {r['qa_avg'] or 0:.2f} |"
        )

    # Tool stats
    if metrics["tool_stats"]:
        lines += ["", "## Tool stats (lean)", "",
                  "| Tool | N | Avg ms | OK | Fail | Cached |",
                  "|---|---|---|---|---|---|"]
        for r in metrics["tool_stats"]:
            lines.append(
                f"| {r['tool_name']} | {r['n']} | {r['avg_ms'] or 0:.0f} | "
                f"{r['ok']} | {r['fail']} | {r['cached']} |"
            )

    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--output", type=Path, default=None,
                    help="Path JSON output (también escribe .md al lado)")
    args = p.parse_args()

    metrics = asyncio.run(fetch_ab_metrics(args.hours))
    if metrics.get("_error"):
        print(f"ERROR: {metrics['_error']}", file=sys.stderr)
        return 1

    md = render_md(metrics)
    print(md)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(metrics, indent=2, default=str, ensure_ascii=False),
                                encoding="utf-8")
        args.output.with_suffix(".md").write_text(md, encoding="utf-8")
        print(f"\n→ guardado: {args.output} y {args.output.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
