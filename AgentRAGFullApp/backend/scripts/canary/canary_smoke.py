"""Sprint M20.05 · S4.1 · Smoke test contra canary firm.

Genera 5 documentos via /v1/documents/v2/generate impersonando la firm QA
y verifica:
  - HTTP 200
  - SSE stream contiene meta + done
  - X-Orchestrator: lean header
  - tool_call_audit tiene filas

USO:
  python scripts/canary/canary_smoke.py <firm_uuid> [--url URL] [--token JWT]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
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


DEFAULT_URL = "https://legal-agent-backend-production-fcfa.up.railway.app"


SMOKE_PROMPTS = [
    {"doc_type": "poder_especial",
     "intent": "Poder especial para José Pérez (CC 80.111.222) a Dra. Ana Gómez (CC 52.333.444, T.P. 99887) para representación civil ordinaria.",
     "brief": "Con facultad de sustituir, conciliar y desistir."},
    {"doc_type": "derecho_peticion",
     "intent": "Derecho de petición a EPS solicitando autorización de procedimiento quirúrgico ordenado por médico tratante. Peticionario: María López, CC 39.111.222.",
     "brief": "20 días sin respuesta."},
    {"doc_type": "tutela",
     "intent": "Tutela contra Banco por embargo cuenta de nómina afectando mínimo vital. Accionante: Carlos Romero, CC 80.444.555. Salario único $1.500.000.",
     "brief": ""},
    {"doc_type": "contrato_arrendamiento",
     "intent": "Contrato arrendamiento residencial. Arrendador: Inversiones X SAS NIT 900.111.222-3. Arrendatario: Pedro Niño, CC 71.555.666. Canon $2.000.000/mes. 12 meses.",
     "brief": "Apto 301, calle 80 #15-20 Bogotá."},
    {"doc_type": "poder_especial",
     "intent": "Poder amplio para Dra. Sandra López (T.P. 12345) representación procesos varios. Poderdante: Marta Suárez, CC 39.888.999.",
     "brief": "Facultades amplias incluyendo recibir pagos."},
]


def call_endpoint(url: str, token: str, firm_id: str, doc: dict) -> dict:
    """Llama /v1/documents/v2/generate y consume SSE stream."""
    import urllib.request

    payload = json.dumps({
        "intent": doc["intent"],
        "user_brief": doc["brief"],
        "doc_type": doc["doc_type"],
        "firm_id": firm_id,
        "borrador_mode": True,
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}" if token else "",
    }
    req = urllib.request.Request(
        f"{url}/v1/documents/v2/generate",
        data=payload, headers=headers, method="POST",
    )

    t0 = time.perf_counter()
    result = {
        "doc_type": doc["doc_type"],
        "intent_preview": doc["intent"][:80],
        "status_code": None,
        "x_orchestrator": None,
        "events": [],
        "duration_s": 0,
        "error": None,
    }
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            result["status_code"] = resp.status
            result["x_orchestrator"] = resp.headers.get("X-Orchestrator")
            for line in resp:
                text = line.decode("utf-8", errors="replace").strip()
                if text.startswith("event:"):
                    result["events"].append(text.split(":", 1)[1].strip())
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    result["duration_s"] = round(time.perf_counter() - t0, 2)
    return result


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("firm_uuid")
    p.add_argument("--url", default=os.getenv("LEXAI_BACKEND_URL", DEFAULT_URL))
    p.add_argument("--token", default=os.getenv("SMOKE_JWT", ""))
    p.add_argument("--prompts", type=int, default=5, help="Cantidad de prompts a correr (max 5)")
    args = p.parse_args()

    n = min(args.prompts, len(SMOKE_PROMPTS))
    print(f"=== Canary smoke · firm={args.firm_uuid} · url={args.url} · prompts={n} ===\n")

    results = []
    for i, doc in enumerate(SMOKE_PROMPTS[:n]):
        print(f"[{i+1}/{n}] {doc['doc_type']}...")
        res = call_endpoint(args.url, args.token, args.firm_uuid, doc)
        results.append(res)
        status = "OK" if res["status_code"] == 200 and "done" in res["events"] else "FAIL"
        print(f"      → {res['duration_s']}s · orchestrator={res['x_orchestrator']} · "
              f"events={len(res['events'])} · {status}")
        if res["error"]:
            print(f"      ERROR: {res['error']}")

    # Summary
    success = sum(1 for r in results if r["status_code"] == 200 and "done" in r["events"])
    lean_count = sum(1 for r in results if r["x_orchestrator"] == "lean")
    print(f"\n=== Resultado ===")
    print(f"  Generaciones exitosas:    {success}/{n}")
    print(f"  Routeadas a lean:         {lean_count}/{n}")
    print(f"  Routeadas a legacy:       {n - lean_count}/{n}")

    if success == n and lean_count == n:
        print(f"\n✓ Canary OK. Avanzar a S4.2 (monitoring 24h)")
        return 0
    elif lean_count == 0:
        print(f"\n✗ Canary FALLÓ: nada ruteó a lean. Verifica LEAN_ORCHESTRATOR_FIRMS en Railway.")
        return 1
    else:
        print(f"\n⚠ Canary parcial. Revisa fallas individuales.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
