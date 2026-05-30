"""Sprint M20.05 · S4.2 · Dashboard ad-hoc de métricas canary.

Query rápida a generation_audit + tool_call_audit filtrando por firm canary
y comparando contra baseline pre-refactor.

USO:
  python scripts/canary/canary_metrics.py <firm_uuid>
  python scripts/canary/canary_metrics.py <firm_uuid> --hours 24
  python scripts/canary/canary_metrics.py <firm_uuid> --baseline tests/fixtures/baseline_metrics_2026_05_29.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
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


async def fetch_metrics(firm_uuid: str, hours: int) -> dict:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        return {"_error": "DATABASE_URL no configurado"}

    import asyncpg
    conn = await asyncpg.connect(dsn)
    try:
        # Métricas agregadas por arm
        ga_query = f"""
            select
              coalesce(orchestrator_kind, 'legacy') as arm,
              count(*) as n,
              avg(duration_seconds) as avg_lat,
              percentile_cont(0.5) within group (order by duration_seconds) as p50_lat,
              percentile_cont(0.95) within group (order by duration_seconds) as p95_lat,
              avg(cost_usd) as avg_cost,
              avg(qa_score) as avg_qa,
              sum(cache_hit_tokens) as total_cache_tokens,
              count(*) filter (where validation_passed = true) as passed_count,
              count(*) filter (where validation_passed = false) as failed_count
            from generation_audit
            where firm_id = $1::uuid
              and created_at > now() - interval '{int(hours)} hours'
            group by orchestrator_kind
        """
        ga_rows = await conn.fetch(ga_query, firm_uuid)

        # tool_call_audit
        tca_query = f"""
            select
              tool_name,
              count(*) as n,
              avg(duration_ms) as avg_ms,
              count(*) filter (where success = true) as ok,
              count(*) filter (where success = false) as fail,
              count(*) filter (where cached = true) as cached,
              avg(tokens_in + tokens_out) filter (where tokens_in is not null) as avg_tokens
            from tool_call_audit
            where firm_id = $1::uuid
              and started_at > now() - interval '{int(hours)} hours'
            group by tool_name
            order by n desc
        """
        try:
            tca_rows = await conn.fetch(tca_query, firm_uuid)
        except Exception:
            tca_rows = []   # tabla aún no aplicada

        # Errores recientes
        errors_query = f"""
            select tool_name, error_class, error_message, count(*) as n
            from tool_call_audit
            where firm_id = $1::uuid
              and success = false
              and started_at > now() - interval '{int(hours)} hours'
            group by tool_name, error_class, error_message
            order by n desc
            limit 10
        """
        try:
            err_rows = await conn.fetch(errors_query, firm_uuid)
        except Exception:
            err_rows = []
    finally:
        await conn.close()

    return {
        "firm_uuid": firm_uuid,
        "hours_window": hours,
        "by_orchestrator": [dict(r) for r in ga_rows],
        "by_tool": [dict(r) for r in tca_rows],
        "recent_errors": [dict(r) for r in err_rows],
    }


def render(metrics: dict, baseline: dict | None = None) -> None:
    if metrics.get("_error"):
        print(f"ERROR: {metrics['_error']}")
        return

    print(f"\n=== Canary metrics · firm={metrics['firm_uuid']} · ventana={metrics['hours_window']}h ===\n")

    by_orch = metrics["by_orchestrator"]
    if not by_orch:
        print("  Sin generaciones en la ventana solicitada.")
        return

    print("[orchestrator agg]")
    print(f"  {'arm':<10} {'n':>5} {'p50_s':>8} {'p95_s':>8} {'avg_cost':>10} {'avg_qa':>8} {'ok':>5} {'err':>5}")
    for row in by_orch:
        print(f"  {row['arm']:<10} {row['n']:>5} {row['p50_lat'] or 0:>8.1f} "
              f"{row['p95_lat'] or 0:>8.1f} ${row['avg_cost'] or 0:>9.4f} "
              f"{row['avg_qa'] or 0:>8.2f} {row['passed_count']:>5} {row['failed_count']:>5}")

    if baseline:
        print(f"\n[vs baseline]")
        b_overall = baseline.get("overall", {})
        b_lat = b_overall.get("latency_s", {}).get("p50")
        b_cost = b_overall.get("cost_usd", {}).get("mean")
        lean = next((r for r in by_orch if r["arm"] == "lean"), None)
        if lean and b_lat:
            d_lat = (lean["p50_lat"] - b_lat) / b_lat * 100
            print(f"  Δ latencia p50 vs baseline: {d_lat:+.1f}%")
        if lean and b_cost:
            d_cost = ((lean["avg_cost"] or 0) - b_cost) / b_cost * 100
            print(f"  Δ costo avg vs baseline:    {d_cost:+.1f}%")

    by_tool = metrics.get("by_tool") or []
    if by_tool:
        print(f"\n[top tools]")
        print(f"  {'tool':<28} {'n':>5} {'avg_ms':>8} {'ok':>5} {'fail':>5} {'cached':>7}")
        for row in by_tool[:15]:
            print(f"  {row['tool_name']:<28} {row['n']:>5} "
                  f"{row['avg_ms'] or 0:>8.0f} {row['ok']:>5} {row['fail']:>5} {row['cached']:>7}")

    err = metrics.get("recent_errors") or []
    if err:
        print(f"\n[errores recientes]")
        for e in err[:5]:
            print(f"  ({e['n']}x) {e['tool_name']} · {e['error_class']}: {(e['error_message'] or '')[:80]}")
    else:
        print(f"\n[errores recientes] ninguno · OK")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("firm_uuid")
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--baseline", type=Path, default=None)
    args = p.parse_args()

    metrics = asyncio.run(fetch_metrics(args.firm_uuid, args.hours))
    baseline = None
    if args.baseline and args.baseline.exists():
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    render(metrics, baseline)
    return 0


if __name__ == "__main__":
    sys.exit(main())
