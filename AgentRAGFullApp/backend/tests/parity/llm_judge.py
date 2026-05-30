"""Sprint M20.04 · S3.3 · LLM-judge comparativo legacy vs lean.

Lee el JSON output del runner_ab y, por cada fixture, llama a Sonnet 4.6
para comparar los textos generados por LEGACY vs LEAN y dar un score 0-10
por dimensión (estilo, contenido legal, citas, coherencia).

USO:
  cd backend
  python -m tests.parity.llm_judge reports/parity_ab_YYYY_MM_DD_HHMMSS.json
"""
from __future__ import annotations

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


JUDGE_SYSTEM = """Eres un juez experto en redacción legal colombiana. Tu tarea
es comparar dos versiones (A=legacy, B=lean) del mismo documento legal y
asignar un puntaje 0-10 por cada dimensión.

DIMENSIONES (cada una 0-10):
  estilo            → registro formal, claridad, fluidez
  contenido_legal   → corrección sustantiva, completitud, exactitud
  citas             → presencia, formato, integración natural
  coherencia        → consistencia interna, cross-refs, sin contradicciones
  estructura        → orden lógico, secciones esperadas presentes

Devuelve JSON ESTRICTO con esta forma:
{
  "winner": "A" | "B" | "tie",
  "summary": "1 frase explicando ganador",
  "scores": {
    "A": {"estilo": N, "contenido_legal": N, "citas": N, "coherencia": N, "estructura": N, "overall": N},
    "B": {"estilo": N, "contenido_legal": N, "citas": N, "coherencia": N, "estructura": N, "overall": N}
  },
  "notes_A": "qué le falta a A",
  "notes_B": "qué le falta a B"
}

NUNCA inventes citas que no aparezcan. NO penalices a una versión por longitud
si la otra también es corta."""


JUDGE_USER_TEMPLATE = """DOC TYPE: {doc_type}
COMPLEJIDAD: {complexity}

=== VERSIÓN A (legacy, 17 stages) ===
{text_a}

=== VERSIÓN B (lean, ReAct + 18 tools) ===
{text_b}

Compara A vs B y emite el JSON."""


async def judge_one(client, fixture: dict) -> dict:
    legacy_runs = fixture.get("legacy_runs") or []
    lean_runs = fixture.get("lean_runs") or []
    if not legacy_runs or not lean_runs:
        return {"_skip": "no hay runs para uno de los arms"}

    # Tomar primera corrida exitosa de cada arm
    legacy_text = next((r.get("full_text_preview", "") for r in legacy_runs if r.get("success")), "")
    lean_text = next((r.get("full_text_preview", "") for r in lean_runs if r.get("success")), "")
    if not legacy_text or not lean_text:
        return {"_skip": "ambos arms deben tener al menos 1 run exitoso"}

    user = JUDGE_USER_TEMPLATE.format(
        doc_type=fixture["doc_type"],
        complexity=fixture.get("complexity", "low"),
        text_a=legacy_text[:6000],
        text_b=lean_text[:6000],
    )
    try:
        resp = await client.messages.create(
            model="claude-sonnet-4-5-20251022",   # fallback a modelo estable si 4-6 no disponible
            max_tokens=1500,
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        text = ""
        for block in resp.content or []:
            if getattr(block, "type", None) == "text":
                text += getattr(block, "text", "")
        # extraer JSON
        import re
        m = re.search(r"\{.*\}", text, re.DOTALL)
        return json.loads(m.group(0)) if m else {"_error": "no JSON", "raw": text[:500]}
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


async def main():
    if len(sys.argv) < 2:
        print("Uso: python -m tests.parity.llm_judge <reports/parity_ab_*.json>")
        return 1

    report_path = Path(sys.argv[1])
    data = json.loads(report_path.read_text(encoding="utf-8"))

    try:
        from utils.llm_provider import _get_anthropic_client
        client = _get_anthropic_client()
    except Exception as e:
        print(f"ERROR: Anthropic client requerido para LLM-judge: {e}")
        return 1
    if client is None:
        print("ERROR: ANTHROPIC_API_KEY no configurado")
        return 1

    print(f"=== LLM-judge sobre {len(data['fixtures'])} fixtures ===\n")

    judged = []
    for fx in data["fixtures"]:
        print(f"  Juzgando {fx['fixture_id']}... ", end="", flush=True)
        verdict = await judge_one(client, fx)
        judged.append({
            "fixture_id": fx["fixture_id"],
            "doc_type": fx["doc_type"],
            "verdict": verdict,
        })
        if verdict.get("_skip"):
            print(f"SKIP ({verdict['_skip']})")
        elif verdict.get("_error"):
            print(f"ERROR ({verdict['_error']})")
        else:
            winner = verdict.get("winner", "?")
            scores = verdict.get("scores", {})
            a_overall = scores.get("A", {}).get("overall", 0)
            b_overall = scores.get("B", {}).get("overall", 0)
            print(f"winner={winner} · A={a_overall:.1f} vs B={b_overall:.1f}")

    out_path = report_path.with_suffix(".judge.json")
    out_path.write_text(json.dumps(judged, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nOK · veredictos guardados: {out_path}")

    # Resumen agregado
    valid = [j["verdict"] for j in judged if j["verdict"].get("scores")]
    if valid:
        wins = {"A": 0, "B": 0, "tie": 0}
        for v in valid:
            wins[v.get("winner", "tie")] = wins.get(v.get("winner", "tie"), 0) + 1
        avg_a = sum(v["scores"]["A"]["overall"] for v in valid) / len(valid)
        avg_b = sum(v["scores"]["B"]["overall"] for v in valid) / len(valid)
        print(f"\nResumen LLM-judge:")
        print(f"  Wins  Legacy(A): {wins['A']}")
        print(f"  Wins  Lean(B):   {wins['B']}")
        print(f"  Tied:            {wins['tie']}")
        print(f"  Avg score A:     {avg_a:.2f}/10")
        print(f"  Avg score B:     {avg_b:.2f}/10")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
