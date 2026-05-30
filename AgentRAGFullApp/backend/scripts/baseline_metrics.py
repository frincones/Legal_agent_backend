"""Sprint M20.01 · Baseline metrics del pipeline actual.

Captura las métricas (latencia, costo, tokens, errores) de las últimas N
generaciones para tener un punto de comparación ANTES del refactor a
LeanOrchestrator + ReAct.

Sin estas métricas no se puede:
  - demostrar que la nueva arquitectura es mejor
  - catch regresiones cuando arranque el A/B testing
  - alimentar el dashboard de costos

Uso:
    # con DATABASE_URL en .env:
    python scripts/baseline_metrics.py [--limit 100] [--output PATH]

    # output por defecto: tests/fixtures/baseline_metrics_YYYY_MM_DD.json

Exit code:
    0 OK (baseline guardado)
    1 fallo (sin DATABASE_URL, sin generaciones, etc.)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---- carga .env ----
_BACKEND_ROOT = Path(__file__).parent.parent

def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

_load_env(_BACKEND_ROOT / ".env")


# ---- pricing actual (USD por 1M tokens) ----
PRICING = {
    "claude-sonnet-4-6":     {"input": 3.00,  "output": 15.00},
    "claude-opus-4-7":       {"input": 15.00, "output": 75.00},
    "gpt-4o":                {"input": 2.50,  "output": 10.00},
    "gpt-4o-mini":           {"input": 0.15,  "output": 0.60},
    "claude-sonnet-3-5":     {"input": 3.00,  "output": 15.00},
    "claude-haiku-3-5":      {"input": 0.80,  "output": 4.00},
}


def _pcts(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p50": None, "p95": None, "p99": None, "mean": None, "count": 0}
    sv = sorted(values)
    n = len(sv)
    return {
        "p50":   sv[int(n * 0.50)],
        "p95":   sv[min(int(n * 0.95), n - 1)],
        "p99":   sv[min(int(n * 0.99), n - 1)],
        "mean":  statistics.mean(sv),
        "count": n,
    }


async def _capture_baseline(limit: int) -> dict[str, Any]:
    """Conecta a Supabase y captura las métricas."""
    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        return {
            "_warning": "DATABASE_URL no encontrado en env. Baseline VACÍO. "
                        "Define DATABASE_URL en .env y vuelve a correr.",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "limit": limit,
            "total_records": 0,
        }

    try:
        import asyncpg
    except ImportError:
        return {"_error": "asyncpg no instalado. pip install asyncpg", "limit": limit}

    conn = await asyncpg.connect(dsn)
    try:
        # --- query principal ---
        rows = await conn.fetch(
            """
            select
              generation_id,
              firm_id,
              template_id,
              model_used,
              duration_seconds,
              cost_usd,
              qa_score,
              validation_passed,
              jsonb_array_length(citations) as citations_count,
              created_at
            from generation_audit
            where created_at > now() - interval '60 days'
            order by created_at desc
            limit $1
            """,
            limit,
        )
        if not rows:
            return {
                "_warning": "No hay registros en generation_audit últimos 60 días.",
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "limit": limit,
                "total_records": 0,
            }

        # --- agregaciones ---
        all_latency = [float(r["duration_seconds"]) for r in rows if r["duration_seconds"]]
        all_cost = [float(r["cost_usd"]) for r in rows if r["cost_usd"]]
        all_qa = [float(r["qa_score"]) for r in rows if r["qa_score"]]
        all_citations = [int(r["citations_count"]) for r in rows]

        # por template
        by_template: dict[str, dict] = {}
        for r in rows:
            t = r["template_id"] or "unknown"
            by_template.setdefault(t, {"latency": [], "cost": [], "qa": [], "passed": 0, "total": 0})
            by_template[t]["latency"].append(float(r["duration_seconds"] or 0))
            by_template[t]["cost"].append(float(r["cost_usd"] or 0))
            if r["qa_score"]:
                by_template[t]["qa"].append(float(r["qa_score"]))
            if r["validation_passed"]:
                by_template[t]["passed"] += 1
            by_template[t]["total"] += 1

        by_template_summary = {
            t: {
                "latency_s": _pcts(d["latency"]),
                "cost_usd":  _pcts(d["cost"]),
                "qa_score":  _pcts(d["qa"]),
                "validation_rate": (d["passed"] / d["total"]) if d["total"] else None,
                "count": d["total"],
            }
            for t, d in by_template.items()
        }

        # detección de modelos usados
        models_used: dict[str, int] = {}
        for r in rows:
            mu = r["model_used"] or {}
            if isinstance(mu, str):
                try:
                    mu = json.loads(mu)
                except Exception:
                    mu = {}
            for stage, model in (mu or {}).items():
                if model:
                    models_used[str(model)] = models_used.get(str(model), 0) + 1

        return {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "source": "generation_audit (last 60 days)",
            "limit": limit,
            "total_records": len(rows),
            "overall": {
                "latency_s":       _pcts(all_latency),
                "cost_usd":        _pcts(all_cost),
                "qa_score":        _pcts(all_qa),
                "citations_count": _pcts([float(c) for c in all_citations]),
            },
            "by_template": by_template_summary,
            "models_used_distribution": models_used,
            "pricing_reference_usd_per_1m_tokens": PRICING,
            "notes": (
                "Baseline pre-refactor a LeanOrchestrator (M20). "
                "Comparar con run post-refactor para validar -55% costo, -60% latencia."
            ),
        }
    finally:
        await conn.close()


def _write_output(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    print(f"OK · baseline guardado en: {path}")
    if "_warning" in data:
        print(f"WARN · {data['_warning']}", file=sys.stderr)
    if "_error" in data:
        print(f"ERROR · {data['_error']}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Captura baseline metrics del pipeline actual")
    parser.add_argument("--limit", type=int, default=100, help="Número de generaciones a analizar (default 100)")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path del JSON output. Default: tests/fixtures/baseline_metrics_YYYY_MM_DD.json",
    )
    args = parser.parse_args()

    if args.output is None:
        today = datetime.now(timezone.utc).strftime("%Y_%m_%d")
        args.output = _BACKEND_ROOT / "tests" / "fixtures" / f"baseline_metrics_{today}.json"

    try:
        data = asyncio.run(_capture_baseline(args.limit))
    except Exception as exc:
        print(f"ERROR capturando baseline: {exc}", file=sys.stderr)
        return 1

    _write_output(data, args.output)
    if data.get("_error"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
