"""Sprint M20.14 · validate_composer_e2e.py · validación HTTP composer → backend.

Simula EXACTAMENTE lo que el composer del frontend hace cuando el usuario envía
un prompt: POST a /v1/documents/v2/generate vía HTTP real (Railway prod o local).

Valida:
  - HTTP status 200 (o 503 si FLAG_DOCGEN_V2=false)
  - Header X-Orchestrator: lean | legacy
  - SSE events recibidos en stream
  - Tools invocadas vía agent_thought
  - Bloques emitidos
  - Done event sin error

NO requiere ANTHROPIC_API_KEY local (el backend Railway tiene la suya).
SÍ requiere un JWT válido de Supabase (cuenta de testing).

USO:
    # Contra Railway prod (default):
    SMOKE_JWT="tu-jwt-de-supabase" python scripts/validate_composer_e2e.py

    # Contra local:
    SMOKE_JWT="..." LEXAI_BACKEND_URL="http://localhost:8000" \\
        python scripts/validate_composer_e2e.py

    # Prompt custom:
    SMOKE_JWT="..." python scripts/validate_composer_e2e.py --prompt "Mi prompt"

    # Sin auth (smoke 401):
    python scripts/validate_composer_e2e.py --smoke-401

Output:
  - Live log SSE events
  - reports/composer_e2e_YYYYMMDD_HHMMSS.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_BACKEND_ROOT = Path(__file__).parent.parent


def _load_env(p: Path) -> None:
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(_BACKEND_ROOT / ".env")
_load_env(Path(r"C:\Users\freddyrs\Desktop\Legal Demo\Legal_agent_Frontend\.env.local"))


DEFAULT_URL = "https://legal-agent-backend-production-fcfa.up.railway.app"


DEFAULT_PROMPT = """Necesito un poder especial para que mi abogada Dra. Sandra
Lopez, CC 98.765.432, T.P. 12345 del C.S. de la J., me represente en proceso
laboral ordinario por reintegro y salarios caidos contra TRANSPORTES LA SABANA
SAS NIT 900.222.333-4.

Poderdante: Andres Felipe Moreno Pinto, CC 80.123.456 de Bogota.

Facultades expresas: sustituir, conciliar, desistir y recibir pagos.
Vigencia: hasta ejecutoria de sentencia.

Citar Arts. 2142 y siguientes del Codigo Civil sobre el regimen del mandato y
los Arts. 75 y 77 del Codigo General del Proceso sobre facultades del apoderado.
"""


def smoke_test_401(url: str) -> int:
    """Verifica que el endpoint existe (responde 401 sin auth)."""
    print("\n=== Smoke test 401 (sin auth) ===")
    req = urllib.request.Request(
        f"{url}/v1/documents/v2/generate", method="POST",
        data=b'{"intent":"x","user_brief":"","doc_type":"poder_especial"}',
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"  [WARN] HTTP {r.status} (esperaba 401)")
            return 1
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print(f"  [OK] HTTP 401 missing_bearer_token (endpoint disponible)")
            return 0
        elif e.code == 503:
            body = e.read().decode("utf-8", errors="replace")[:200]
            print(f"  [WARN] HTTP 503 ({body}) · FLAG_DOCGEN_V2=false en Railway")
            return 2
        else:
            print(f"  [FAIL] HTTP {e.code}")
            return 1
    except Exception as e:
        print(f"  [FAIL] {type(e).__name__}: {e}")
        return 1


def parse_sse_line(text: str) -> tuple[str | None, dict | None]:
    """Parse 1 SSE event_name + json data (acumula multi-line si fuera necesario)."""
    if text.startswith("event:"):
        return text.split(":", 1)[1].strip(), None
    if text.startswith("data:"):
        try:
            return None, json.loads(text[5:].strip())
        except Exception:
            return None, {"_raw": text[5:].strip()[:200]}
    return None, None


def call_endpoint(url: str, jwt: str, prompt: str, doc_type: str) -> dict:
    """Llama /v1/documents/v2/generate y procesa SSE stream."""
    payload = json.dumps({
        "intent": prompt,
        "user_brief": "",
        "doc_type": doc_type,
        "borrador_mode": True,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{url}/v1/documents/v2/generate", method="POST",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {jwt}",
        },
    )

    result = {
        "url": f"{url}/v1/documents/v2/generate",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "duration_s": 0.0,
        "http_status": None,
        "x_orchestrator": None,
        "headers": {},
        "events": [],
        "event_counts": {},
        "tools_invoked": [],
        "blocks_count": 0,
        "citations_count": 0,
        "iterations": 0,
        "error": None,
    }
    t0 = time.perf_counter()
    try:
        resp = urllib.request.urlopen(req, timeout=300)
        result["http_status"] = resp.status
        result["x_orchestrator"] = resp.headers.get("x-orchestrator", "(no-header)")
        result["headers"] = {
            "x-orchestrator": result["x_orchestrator"],
            "content-type": resp.headers.get("content-type", ""),
            "cache-control": resp.headers.get("cache-control", ""),
        }
        print(f"\n  [HTTP] {result['http_status']} · X-Orchestrator: {result['x_orchestrator']}\n")

        current_event = None
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            ev, data = parse_sse_line(line)
            if ev:
                current_event = ev
                result["event_counts"][ev] = result["event_counts"].get(ev, 0) + 1
            elif data is not None and current_event:
                # log
                payload_preview = json.dumps(data, default=str)[:120]
                elapsed = time.perf_counter() - t0
                if current_event in ("agent_thought", "block_emit", "section_started",
                                       "section_done", "meta", "done", "error"):
                    print(f"  [{elapsed:6.1f}s] {current_event} · {payload_preview}")
                result["events"].append({"t_s": round(elapsed, 1),
                                          "name": current_event,
                                          "payload": data})
                # tracking
                if current_event == "agent_thought":
                    tool = data.get("tool")
                    if tool and tool not in result["tools_invoked"]:
                        result["tools_invoked"].append(tool)
                if current_event == "block_emit":
                    result["blocks_count"] += 1
                if current_event == "citation_verify":
                    result["citations_count"] += 1
                if current_event == "stage_progress" and "brain_iter" in data.get("stage", ""):
                    result["iterations"] += 1
                if current_event == "error":
                    result["error"] = data
    except urllib.error.HTTPError as e:
        result["http_status"] = e.code
        body = e.read().decode("utf-8", errors="replace")
        result["error"] = {"http_status": e.code, "body": body[:500]}
        print(f"  [FAIL] HTTP {e.code} · {body[:200]}")
    except Exception as e:
        result["error"] = {"exception": f"{type(e).__name__}: {e}"}
        print(f"  [FAIL] {type(e).__name__}: {e}")
    finally:
        result["duration_s"] = round(time.perf_counter() - t0, 1)
    return result


def analyze(result: dict) -> tuple[int, int]:
    """Retorna (passed, total)."""
    print("\n" + "=" * 75)
    print(f"ANALISIS · status={result['http_status']} duration={result['duration_s']}s")
    print("=" * 75)
    checks = []

    def chk(name: str, cond: bool, detail: str = "") -> None:
        icon = "[OK]" if cond else "[FAIL]"
        print(f"  {icon} {name}{' · ' + detail if detail else ''}")
        checks.append(cond)

    chk("HTTP 200", result["http_status"] == 200)
    chk("X-Orchestrator header presente",
        result["x_orchestrator"] not in (None, "(no-header)"),
        f"= {result['x_orchestrator']}")
    chk("meta event recibido", result["event_counts"].get("meta", 0) >= 1)
    chk("done event recibido (sin error)",
        result["event_counts"].get("done", 0) >= 1 and not result["error"])
    chk(">= 3 tools invocadas", len(result["tools_invoked"]) >= 3,
        f"= {result['tools_invoked']}")
    chk(">= 1 bloque emitido", result["blocks_count"] >= 1,
        f"= {result['blocks_count']}")
    chk(">= 5 tipos de evento SSE", len(result["event_counts"]) >= 5,
        f"= {len(result['event_counts'])} tipos")

    # Específicos del lean
    if result["x_orchestrator"] == "lean":
        chk(">= 2 iteraciones ReAct", result["iterations"] >= 2,
            f"= {result['iterations']}")
        chk("verify_citation invocada (>= 1)",
            "verify_citation" in result["tools_invoked"])

    passed = sum(1 for c in checks if c)
    total = len(checks)
    print(f"\nResultado: {passed}/{total} pass")
    if passed == total:
        print(f"\n[100% PASS] composer -> backend -> {result['x_orchestrator']} orchestrator OK")
    elif passed >= total * 0.7:
        print(f"\n[OK con observaciones] revisar fallas individuales")
    else:
        print(f"\n[FAIL] flow composer->backend tiene problemas")
    return passed, total


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=os.getenv("LEXAI_BACKEND_URL", DEFAULT_URL))
    p.add_argument("--jwt", default=os.getenv("SMOKE_JWT", ""))
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    p.add_argument("--doc-type", default="poder_especial")
    p.add_argument("--smoke-401", action="store_true",
                    help="Solo verifica endpoint vivo (401 sin auth)")
    args = p.parse_args()

    print("=" * 75)
    print(f"validate_composer_e2e.py · {datetime.now(timezone.utc).isoformat()}")
    print(f"  url:      {args.url}")
    print(f"  doc_type: {args.doc_type}")
    print(f"  jwt:      {'<set, len=' + str(len(args.jwt)) + '>' if args.jwt else 'EMPTY'}")
    print(f"  prompt:   {args.prompt[:80]}...")
    print("=" * 75)

    smoke_code = smoke_test_401(args.url)
    if smoke_code == 2:
        print("\n[WARN] endpoint v2 desactivado · setear FLAG_DOCGEN_V2=true en Railway")
        return 2

    if args.smoke_401:
        return smoke_code

    if not args.jwt:
        print("\n[INFO] Sin JWT, solo se hizo smoke 401. Para test full:")
        print("  SMOKE_JWT='tu-jwt-supabase' python scripts/validate_composer_e2e.py")
        return smoke_code

    print(f"\n=== Test full E2E ===")
    result = call_endpoint(args.url, args.jwt, args.prompt, args.doc_type)
    passed, total = analyze(result)

    # persistir
    reports_dir = _BACKEND_ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_path = reports_dir / f"composer_e2e_{ts}.json"
    report_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False),
                            encoding="utf-8")
    print(f"\n[reporte] {report_path}")

    return 0 if passed >= total * 0.7 else 1


if __name__ == "__main__":
    sys.exit(main())
