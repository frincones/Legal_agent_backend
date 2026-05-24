#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Sprint M15 · Eval script para el VerificationAgent contra gold set.

Uso:
  python scripts/eval_citation_gold_set.py            # smoke local (sin BD)
  python scripts/eval_citation_gold_set.py --prod     # contra Railway prod

Métricas:
  - CER (Citation Existence Rate): citas reales verificadas / total reales
  - FPR (False Positive Rate): inválidas marcadas verified / total inválidas
  - FNR (False Negative Rate): reales marcadas no_encontrada / total reales
  - Latencia p50/p95
  - Costo (estimado por uso de LLM normalizer)
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# Permite ejecutar desde backend/
sys.path.insert(0, str(Path(__file__).parent.parent))

# UTF-8 stdout on Windows
if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from tests.fixtures.gold_set import GOLD_SET, get_gold_set_summary


async def run_eval(use_prod: bool = False) -> dict:
    """Ejecuta el gold set contra el VerificationAgent."""
    print("=" * 70)
    print("GOLD SET EVAL — Sprint M15")
    print("=" * 70)
    summary = get_gold_set_summary()
    print(f"Total: {summary['total']} citas")
    print(f"By estado: {summary['by_estado']}")
    print(f"By kind: {summary['by_kind']}")
    print()

    if use_prod:
        from utils.db import get_storage
        from utils.llm import get_openai_client
        storage = await get_storage()
        client = get_openai_client()
        pool = storage.pool
    else:
        print("MODE: local smoke (sin BD ni LLM real)")
        from utils.citation_verifier import parse_citation_ref
        # Solo testear el parser sin verificar contra BD
        results = []
        for ref, expected, kind, source in GOLD_SET:
            t0 = time.time()
            parsed = parse_citation_ref(ref)
            dt = (time.time() - t0) * 1000
            results.append({
                "ref": ref,
                "expected": expected,
                "parsed": parsed.kind if parsed else "unparseable",
                "duration_ms": dt,
            })
        # Solo verificar que el parser captura el kind correcto
        parser_ok = sum(1 for r in results if r["parsed"] != "unparseable")
        print(f"\nParser captura: {parser_ok}/{len(results)} citas parseables")
        for r in results:
            if r["parsed"] == "unparseable":
                print(f"  ⚠️  unparseable: {r['ref']!r}")
        return {"mode": "local", "parser_ok": parser_ok, "total": len(results)}

    # MODE PROD
    from lex.verify.verification_agent import VerificationAgent
    agent = VerificationAgent(client, pool)

    results = []
    print(f"\nEjecutando contra producción...")
    started_total = time.time()
    for i, (ref, expected, kind, source) in enumerate(GOLD_SET, 1):
        verdict = await agent.verify(ref, kind)
        match = (verdict.estado == expected)
        marker = "✓" if match else "✗"
        print(f"  [{i:2d}/{len(GOLD_SET)}] {marker} {ref!r:40s} -> {verdict.estado:15s} (expected {expected}, conf {verdict.confidence:.2f})")
        results.append({
            "ref": ref, "expected": expected, "actual": verdict.estado,
            "match": match, "confidence": verdict.confidence,
            "duration_ms": verdict.duration_ms,
            "method": verdict.method,
        })

    total_dt = (time.time() - started_total) * 1000

    # Métricas
    reales = [r for r in results if r["expected"] != "no_encontrada"]
    invalidas = [r for r in results if r["expected"] == "no_encontrada"]
    verificadas_reales = sum(1 for r in reales if r["actual"] == "verificada")
    fp = sum(1 for r in invalidas if r["actual"] in ("verificada", "sospechosa"))
    fn = sum(1 for r in reales if r["actual"] == "no_encontrada")

    cer = verificadas_reales / len(reales) if reales else 0
    fpr = fp / len(invalidas) if invalidas else 0
    fnr = fn / len(reales) if reales else 0

    durations = sorted(r["duration_ms"] for r in results)
    p50 = durations[len(durations) // 2]
    p95 = durations[int(len(durations) * 0.95)]

    print()
    print("=" * 70)
    print("RESULTADOS")
    print("=" * 70)
    print(f"CER (Citation Existence Rate):  {cer*100:.1f}% ({verificadas_reales}/{len(reales)})")
    print(f"FPR (False Positive Rate):      {fpr*100:.1f}% ({fp}/{len(invalidas)})")
    print(f"FNR (False Negative Rate):      {fnr*100:.1f}% ({fn}/{len(reales)})")
    print(f"Latencia p50:                   {p50:.0f}ms")
    print(f"Latencia p95:                   {p95:.0f}ms")
    print(f"Total time:                     {total_dt/1000:.1f}s")

    targets_met = (cer >= 0.95 and fpr <= 0.05 and fnr <= 0.05)
    print()
    print(f"TARGETS MET: {'✅ YES' if targets_met else '❌ NO'}")
    print("  CER >= 95%:", "✓" if cer >= 0.95 else "✗")
    print("  FPR <= 5%:", "✓" if fpr <= 0.05 else "✗")
    print("  FNR <= 5%:", "✓" if fnr <= 0.05 else "✗")

    return {
        "cer": cer, "fpr": fpr, "fnr": fnr,
        "p50": p50, "p95": p95,
        "total_seconds": total_dt / 1000,
        "targets_met": targets_met,
    }


if __name__ == "__main__":
    use_prod = "--prod" in sys.argv
    result = asyncio.run(run_eval(use_prod=use_prod))
    sys.exit(0 if result.get("targets_met", True) else 1)
