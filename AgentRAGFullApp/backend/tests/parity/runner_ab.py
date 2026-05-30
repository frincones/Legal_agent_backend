"""Sprint M20.04 · S3.2 · Runner A/B legacy vs lean.

Para cada fixture en parity_fixtures_v1.json:
  - Corre N veces el orchestrator LEGACY (sin tocar Anthropic flag)
  - Corre N veces el LeanOrchestrator (USE_LEAN_ORCHESTRATOR=true forzado)
  - Mide: latencia, tokens, costo, # blocks, # citations verificadas, errores
  - Genera DOCX final y lo guarda en /tmp/parity/{fixture_id}_{arm}_{run}.docx
  - Llama LLM-judge para comparar quality side-by-side

Output:
  reports/parity_ab_YYYY_MM_DD_HHMMSS.json  → métricas completas
  reports/parity_ab_YYYY_MM_DD_HHMMSS.md    → tabla resumen humano-legible

PRE-REQUISITOS:
  - DATABASE_URL en .env
  - ANTHROPIC_API_KEY en .env (Sonnet 4.6 mínimo)
  - OPENAI_API_KEY en .env (gpt-4o-mini para legacy + LLM-judge)
  - Migración M20.01 aplicada en Supabase

USO:
  cd backend
  python -m tests.parity.runner_ab                    # corre todos, 1 run c/u
  python -m tests.parity.runner_ab --limit 5          # solo primeros 5
  python -m tests.parity.runner_ab --runs 3           # 3 corridas por fixture
  python -m tests.parity.runner_ab --arm lean         # solo lean
  python -m tests.parity.runner_ab --arm legacy       # solo legacy
  python -m tests.parity.runner_ab --fixture poder_1_basico_civil --runs 5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

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


# ---- Tipos ----

Arm = Literal["legacy", "lean"]


@dataclass
class RunResult:
    fixture_id: str
    arm: Arm
    run_index: int
    started_at: str
    ended_at: str
    duration_seconds: float
    success: bool
    error: str | None = None
    sse_events_count: int = 0
    event_names: list[str] = field(default_factory=list)
    blocks_count: int = 0
    citations_count: int = 0
    citations_grounded: int = 0
    citations_derogada: int = 0
    citations_not_found: int = 0
    citations_verify_flag: int = 0
    tokens_total: int = 0
    cost_usd: float = 0.0
    docx_path: str | None = None
    docx_size_kb: int | None = None
    full_text_preview: str = ""


@dataclass
class FixtureSummary:
    fixture_id: str
    doc_type: str
    complexity: str
    legacy_runs: list[RunResult] = field(default_factory=list)
    lean_runs: list[RunResult] = field(default_factory=list)

    def stats_for(self, arm: Arm) -> dict:
        runs = self.legacy_runs if arm == "legacy" else self.lean_runs
        if not runs:
            return {"count": 0}
        ok_runs = [r for r in runs if r.success]
        return {
            "count": len(runs),
            "success_count": len(ok_runs),
            "success_rate": len(ok_runs) / len(runs),
            "latency_p50": statistics.median([r.duration_seconds for r in ok_runs]) if ok_runs else None,
            "latency_p95": _p95([r.duration_seconds for r in ok_runs]) if ok_runs else None,
            "tokens_avg": statistics.mean([r.tokens_total for r in ok_runs]) if ok_runs else 0,
            "cost_avg": statistics.mean([r.cost_usd for r in ok_runs]) if ok_runs else 0,
            "blocks_avg": statistics.mean([r.blocks_count for r in ok_runs]) if ok_runs else 0,
            "citations_grounded_avg": statistics.mean([r.citations_grounded for r in ok_runs]) if ok_runs else 0,
            "errors": [r.error for r in runs if not r.success and r.error],
        }


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    sv = sorted(values)
    return sv[min(int(len(sv) * 0.95), len(sv) - 1)]


# ---- Runner core ----

class ABRunner:
    def __init__(self, fixtures: list[dict], runs: int, output_dir: Path):
        self.fixtures = fixtures
        self.runs = runs
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.docx_dir = output_dir / "docx"
        self.docx_dir.mkdir(parents=True, exist_ok=True)

    async def run(self, arms: list[Arm]) -> list[FixtureSummary]:
        from utils.db import get_storage
        storage = await get_storage()
        pool = storage.pool

        summaries: list[FixtureSummary] = []
        for fx in self.fixtures:
            summary = FixtureSummary(
                fixture_id=fx["id"],
                doc_type=fx["doc_type"],
                complexity=fx.get("complexity", "low"),
            )
            for arm in arms:
                print(f"\n=== {fx['id']} | arm={arm} | doc_type={fx['doc_type']} ===")
                for i in range(self.runs):
                    print(f"  run {i + 1}/{self.runs}...", flush=True)
                    res = await self._run_one(fx, arm, i, pool)
                    if arm == "legacy":
                        summary.legacy_runs.append(res)
                    else:
                        summary.lean_runs.append(res)
                    status = "OK" if res.success else f"ERR ({res.error[:60] if res.error else ''})"
                    print(f"    → {res.duration_seconds:.1f}s · {res.blocks_count} blocks · "
                          f"{res.citations_grounded}/{res.citations_count} citas grounded · "
                          f"${res.cost_usd:.4f} · {status}", flush=True)
            summaries.append(summary)

        return summaries

    async def _run_one(self, fx: dict, arm: Arm, run_index: int, pool) -> RunResult:
        from utils.llm import get_openai_client

        started = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        result = RunResult(
            fixture_id=fx["id"],
            arm=arm,
            run_index=run_index,
            started_at=started.isoformat(),
            ended_at="",
            duration_seconds=0,
            success=False,
        )

        try:
            generator = self._build_generator(fx, arm, pool, get_openai_client())
            blocks_count = 0
            citations_grounded = 0
            citations_total = 0
            citations_derogada = 0
            citations_not_found = 0
            citations_verify_flag = 0
            tokens_total = 0
            cost_usd = 0.0
            docx_path = None
            docx_size = None
            full_text_chunks: list[str] = []
            event_names: list[str] = []
            sse_count = 0

            async for sse_bytes in generator:
                sse_count += 1
                text = sse_bytes.decode("utf-8", errors="replace")
                if not text.startswith("event:"):
                    continue
                lines = text.strip().split("\n")
                ev_name = lines[0].split(":", 1)[1].strip()
                event_names.append(ev_name)
                payload = {}
                for line in lines[1:]:
                    if line.startswith("data:"):
                        try:
                            payload = json.loads(line[5:].strip())
                        except Exception:
                            payload = {}

                if ev_name == "block_emit":
                    blocks_count += 1
                    block = payload.get("block", {})
                    for run in block.get("runs", []) or []:
                        if isinstance(run, dict) and "text" in run:
                            full_text_chunks.append(run["text"])
                    for k in ("text", "ratio", "contenido"):
                        if isinstance(block.get(k), str):
                            full_text_chunks.append(block[k])

                if ev_name == "citation_verify":
                    citations_total += 1
                    tier = payload.get("tier")
                    if tier == "GROUNDED" or payload.get("found"):
                        citations_grounded += 1
                    elif tier == "DEROGADA":
                        citations_derogada += 1
                    elif tier == "NOT_FOUND":
                        citations_not_found += 1
                    elif tier == "VERIFY_FLAG":
                        citations_verify_flag += 1

                if ev_name == "done":
                    cost_usd = float(payload.get("cost_usd") or 0)
                    tokens_total = int(payload.get("tokens_input") or 0) + int(payload.get("tokens_output") or 0)

                if ev_name == "presented_file":
                    docx_size = payload.get("size_kb")
                if ev_name == "docx_built":
                    docx_size = docx_size or payload.get("size_kb")

            result.blocks_count = blocks_count
            result.citations_count = citations_total
            result.citations_grounded = citations_grounded
            result.citations_derogada = citations_derogada
            result.citations_not_found = citations_not_found
            result.citations_verify_flag = citations_verify_flag
            result.tokens_total = tokens_total
            result.cost_usd = cost_usd
            result.docx_path = docx_path
            result.docx_size_kb = docx_size
            result.sse_events_count = sse_count
            result.event_names = event_names[:30]  # primeros 30 para auditoría
            result.full_text_preview = "".join(full_text_chunks)[:3000]
            result.success = True
        except Exception as e:
            result.error = f"{type(e).__name__}: {str(e)[:300]}"
            traceback.print_exc()

        result.duration_seconds = round(time.perf_counter() - t0, 2)
        result.ended_at = datetime.now(timezone.utc).isoformat()
        return result

    def _build_generator(self, fx: dict, arm: Arm, pool, openai_client):
        firm_id = uuid4()  # firm de test (no afecta producción gracias a RLS)
        generation_id = uuid4()

        if arm == "lean":
            # Forzar Lean (bypass feature flag)
            from lex.orchestrator.lean_orchestrator import LeanOrchestrator
            from utils.llm_provider import _get_anthropic_client
            anth = _get_anthropic_client()
            if anth is None:
                raise RuntimeError("Anthropic client no inicializado (configurar ANTHROPIC_API_KEY)")
            orchestrator = LeanOrchestrator(
                anthropic_client=anth,
                openai_client=openai_client,
                pool=pool,
                firm_id=firm_id,
                user_id=None,
                generation_id=generation_id,
            )
            return orchestrator.run(
                intent=fx["intent"],
                brief=fx.get("user_brief", ""),
                doc_type_hint=fx.get("doc_type", ""),
                matter_id=None,
            )

        # Legacy path
        from lex.orchestrator import GenerationRequest, run_pipeline
        req = GenerationRequest(
            intent=fx["intent"],
            user_brief=fx.get("user_brief", ""),
            matter_id=None,
            firm_id=str(firm_id),
            materia=None,
            doc_type=fx.get("doc_type"),
            context={},
            borrador_mode=True,
        )
        return run_pipeline(openai_client, pool, req)


# ---- Reportes ----

def write_json_report(summaries: list[FixtureSummary], output_path: Path) -> None:
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary_count": len(summaries),
        "fixtures": [
            {
                "fixture_id": s.fixture_id,
                "doc_type": s.doc_type,
                "complexity": s.complexity,
                "stats_legacy": s.stats_for("legacy"),
                "stats_lean": s.stats_for("lean"),
                "legacy_runs": [asdict(r) for r in s.legacy_runs],
                "lean_runs": [asdict(r) for r in s.lean_runs],
            }
            for s in summaries
        ],
    }
    output_path.write_text(json.dumps(data, indent=2, default=str, ensure_ascii=False),
                            encoding="utf-8")
    print(f"\nOK · reporte JSON: {output_path}")


def write_md_report(summaries: list[FixtureSummary], output_path: Path) -> None:
    rows = ["# Reporte A/B paridad legacy vs lean", "",
            f"**Generado:** {datetime.now(timezone.utc).isoformat()}",
            f"**Fixtures:** {len(summaries)}", "",
            "## Resumen comparativo",
            "",
            "| Fixture | Doc Type | Complex | Lat L | Lat Le | Δ Lat | Cost L | Cost Le | Δ Cost | Blocks L | Blocks Le | Grounded L | Grounded Le |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in summaries:
        sl = s.stats_for("legacy")
        sln = s.stats_for("lean")
        lat_l = sl.get("latency_p50") or 0
        lat_le = sln.get("latency_p50") or 0
        delta_lat = (lat_le - lat_l) / lat_l * 100 if lat_l else 0
        cost_l = sl.get("cost_avg") or 0
        cost_le = sln.get("cost_avg") or 0
        delta_cost = (cost_le - cost_l) / cost_l * 100 if cost_l else 0
        rows.append(
            f"| {s.fixture_id} | {s.doc_type} | {s.complexity} | "
            f"{lat_l:.1f}s | {lat_le:.1f}s | {delta_lat:+.0f}% | "
            f"${cost_l:.4f} | ${cost_le:.4f} | {delta_cost:+.0f}% | "
            f"{sl.get('blocks_avg', 0):.0f} | {sln.get('blocks_avg', 0):.0f} | "
            f"{sl.get('citations_grounded_avg', 0):.1f} | {sln.get('citations_grounded_avg', 0):.1f} |"
        )
    rows += ["", "## Totales", ""]
    legacy_lats = [s.stats_for("legacy").get("latency_p50") or 0 for s in summaries]
    lean_lats = [s.stats_for("lean").get("latency_p50") or 0 for s in summaries]
    legacy_costs = [s.stats_for("legacy").get("cost_avg") or 0 for s in summaries]
    lean_costs = [s.stats_for("lean").get("cost_avg") or 0 for s in summaries]

    def _avg(xs): return sum(xs) / len(xs) if xs else 0
    rows.append(f"- **Latencia promedio legacy:** {_avg(legacy_lats):.1f}s")
    rows.append(f"- **Latencia promedio lean:** {_avg(lean_lats):.1f}s")
    rows.append(f"- **Reducción latencia:** {((_avg(lean_lats)-_avg(legacy_lats))/_avg(legacy_lats)*100) if _avg(legacy_lats) else 0:+.0f}%")
    rows.append(f"- **Costo promedio legacy:** ${_avg(legacy_costs):.4f}")
    rows.append(f"- **Costo promedio lean:** ${_avg(lean_costs):.4f}")
    rows.append(f"- **Reducción costo:** {((_avg(lean_costs)-_avg(legacy_costs))/_avg(legacy_costs)*100) if _avg(legacy_costs) else 0:+.0f}%")

    rows.append("")
    rows.append("## Acceptance gates · ver tests/parity/ACCEPTANCE.md")
    output_path.write_text("\n".join(rows), encoding="utf-8")
    print(f"OK · reporte MD: {output_path}")


# ---- CLI ----

def _load_fixtures() -> list[dict]:
    path = _BACKEND_ROOT / "tests" / "fixtures" / "requests" / "parity_fixtures_v1.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["fixtures"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Solo los primeros N fixtures")
    parser.add_argument("--runs", type=int, default=1, help="Corridas por fixture (default 1)")
    parser.add_argument("--arm", choices=["legacy", "lean", "both"], default="both")
    parser.add_argument("--fixture", type=str, default=None, help="ID específico a correr")
    parser.add_argument("--output-dir", type=Path,
                        default=_BACKEND_ROOT / "reports")
    args = parser.parse_args()

    fixtures = _load_fixtures()
    if args.fixture:
        fixtures = [f for f in fixtures if f["id"] == args.fixture]
        if not fixtures:
            print(f"ERROR: fixture {args.fixture!r} no encontrado")
            return 1
    if args.limit:
        fixtures = fixtures[:args.limit]

    arms: list[Arm] = ["legacy", "lean"] if args.arm == "both" else [args.arm]   # type: ignore

    print(f"=== Runner A/B paridad legacy vs lean ===")
    print(f"  fixtures: {len(fixtures)} | runs: {args.runs} | arms: {arms}")
    print(f"  output: {args.output_dir}\n")

    runner = ABRunner(fixtures, args.runs, args.output_dir)
    summaries = asyncio.run(runner.run(arms))

    ts = datetime.now(timezone.utc).strftime("%Y_%m_%d_%H%M%S")
    json_path = args.output_dir / f"parity_ab_{ts}.json"
    md_path = args.output_dir / f"parity_ab_{ts}.md"
    write_json_report(summaries, json_path)
    write_md_report(summaries, md_path)
    print(f"\n=== Done · {len(summaries)} fixtures procesados ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
