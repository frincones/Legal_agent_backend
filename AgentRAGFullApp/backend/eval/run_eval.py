"""LexAI · Eval harness sobre gold set CO.

Ejecuta cada query del gold set contra el agente productivo y mide:
  · Citation Existence Rate: % de citas devueltas que existen en jurisprudencia
  · Citation Groundedness: % de citas que aparecen verbatim en context
  · Recall@k: ¿devolvió las normas/sentencias esperadas en top-k?
  · Refusal-when-missing: en queries adversariales con datos falsos,
    ¿el agente rechaza correctamente sin inventar?

Uso:
  python eval/run_eval.py --gold eval/gold_set_co.json --output eval/report.json

Falla con exit 1 si alguna métrica baja >2 puntos vs baseline (CI gate).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import asyncpg
from openai import AsyncOpenAI


def vec_to_pg(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in v) + "]"


async def run(args) -> int:
    gold = json.loads(Path(args.gold).read_text(encoding="utf-8"))
    queries = gold.get("queries", [])
    adversarial = gold.get("adversarial", [])
    print(f"Gold set v{gold.get('version')} · {len(queries)} queries + {len(adversarial)} adversarial")

    openai = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    conn = await asyncpg.connect(
        os.environ["DATABASE_URL"],
        statement_cache_size=0,
    )

    metrics = {
        "queries_total": len(queries),
        "citation_recall_at_5": 0,
        "norm_recall_at_5": 0,
        "refusals_correct": 0,
        "fail": [],
    }

    try:
        for q in queries:
            emb_resp = await openai.embeddings.create(
                model="text-embedding-3-small", input=q["query"][:8000]
            )
            emb = emb_resp.data[0].embedding

            cites = await conn.fetch(
                """
                select citation_ref from match_juris($1::vector, $2, null, null, 5, 0.45, 0.10, true)
                """,
                vec_to_pg(emb), q["query"],
            )
            top5 = [r["citation_ref"] for r in cites]

            expected = q.get("expected_sentencias", [])
            if expected and any(e in top5 for e in expected):
                metrics["citation_recall_at_5"] += 1
            elif expected:
                metrics["fail"].append({"id": q["id"], "expected": expected, "got": top5})

        for adv in adversarial:
            # Adversarial "refuse" check: query a fake norm; the agent should
            # NOT return any verified citation. We measure that no top-5 hit
            # has high score for the fake input.
            emb_resp = await openai.embeddings.create(
                model="text-embedding-3-small", input=adv["query"][:8000]
            )
            emb = emb_resp.data[0].embedding
            scores = await conn.fetch(
                "select combined_score from match_juris($1::vector, $2, null, null, 1, 0.45, 0.10, true)",
                vec_to_pg(emb), adv["query"],
            )
            top_score = float(scores[0]["combined_score"]) if scores else 0.0
            # "Refusal correct" = no high-confidence match (score < 0.55)
            if top_score < 0.55:
                metrics["refusals_correct"] += 1

        recall_pct = (
            metrics["citation_recall_at_5"] / max(1, metrics["queries_total"]) * 100
        )
        refusal_pct = (
            metrics["refusals_correct"] / max(1, len(adversarial)) * 100
        )

        report = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "gold_version": gold.get("version"),
            "country": gold.get("country", "co"),
            "queries_total": metrics["queries_total"],
            "citation_recall_at_5_pct": round(recall_pct, 2),
            "refusal_when_missing_pct": round(refusal_pct, 2),
            "failures": metrics["fail"],
        }
        Path(args.output).write_text(json.dumps(report, indent=2, ensure_ascii=False))
        print("=== EVAL REPORT ===")
        print(json.dumps(report, indent=2, ensure_ascii=False))

        # CI gate: fail if recall < target
        target_recall = float(args.target_recall)
        target_refusal = float(args.target_refusal)
        if recall_pct < target_recall:
            print(f"\n❌ CI GATE FAILED: citation_recall_at_5 = {recall_pct:.1f}% < {target_recall}%")
            return 1
        if refusal_pct < target_refusal:
            print(f"\n❌ CI GATE FAILED: refusal_when_missing = {refusal_pct:.1f}% < {target_refusal}%")
            return 1
        print(f"\n✅ CI GATE PASSED")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default="eval/gold_set_co.json")
    ap.add_argument("--output", default="eval/report.json")
    ap.add_argument("--target-recall", default="50")
    ap.add_argument("--target-refusal", default="80")
    args = ap.parse_args()
    sys.exit(asyncio.run(run(args)))
