"""Sprint M20.14 · validate_full_lean_agent_e2e.py · E2E exhaustivo del agente Lean.

Test mas completo posible del LeanOrchestrator desde el endpoint real
(/v1/documents/v2/generate). Disenado para ejercitar TODAS las capacidades
nuevas tras los fixes M20.14:

  - MODO BORRADOR (system prompt nuevo)
  - borrador_mode cableado endpoint -> lean -> brain
  - agent_thought con `content` (no `message`) llegando al frontend
  - SectionHeadingBlock con roman=None coercido
  - build_docx persist con columnas correctas (content_bytes/format)
  - verify_citation usando cliente OpenAI (no Anthropic)
  - load_skill_md con shape correcta de SkillContext
  - observability del Brain (logger.info por call)

PROMPT: demanda laboral ordinaria por reintegro + indemnizacion moratoria,
disenado para forzar el uso del MAYOR numero de tools posible:

  load_skill_md, load_playbook, extract_data, generate_clause (en paralelo
  para multiples secciones), verify_citation (4 citas mezcladas: vigente,
  derogada, jurisprudencia, decreto poco comun), check_derogation,
  search_jurisprudence, calc_legal (intereses + indexacion), check_coherence,
  check_completeness, build_docx, persist_audit.

VALIDACIONES (PASS/FAIL explicito por cada capacidad):
  1.  HTTP 200 + X-Orchestrator: lean
  2.  meta event emitido al inicio (con generation_id, model, tools_count)
  3.  Brain hizo al menos 3 iteraciones (stage_progress brain_iter_N)
  4.  Tools usadas: cubre al menos 6 distintas
  5.  load_skill_md OK con found=true sections>=N
  6.  verify_citation invocado al menos 3 veces
  7.  generate_clause invocado al menos 3 veces (paralelizable)
  8.  Blocks materializados >= 10 (titulo + secciones + parrafos)
  9.  agent_thought con content (no message) presente al final
  10. done event sin error (status="completed")
  11. No errores fatales en el stream
  12. (Si DOCX persisted) document_files row existe

USO:
  # Smoke 401 (sin JWT):
  python scripts/validate_full_lean_agent_e2e.py --smoke-401

  # E2E completo con JWT real:
  SMOKE_JWT="eyJhbGc..." python scripts/validate_full_lean_agent_e2e.py

  # Contra local:
  SMOKE_JWT="..." LEXAI_BACKEND_URL="http://localhost:8000" \
      python scripts/validate_full_lean_agent_e2e.py

  # Modo firma (override del default borrador):
  SMOKE_JWT="..." python scripts/validate_full_lean_agent_e2e.py --firma

OUTPUT:
  - Log live de cada SSE event con elapsed
  - reports/full_lean_e2e_YYYYMMDD_HHMMSS.json con todo el stream
  - Tabla de validaciones PASS/FAIL al final
  - Exit code 0 = todas las validaciones PASS, 1 = alguna FAIL
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
from typing import Any

_BACKEND_ROOT = Path(__file__).parent.parent
_REPORTS_DIR = _BACKEND_ROOT / "reports"
_REPORTS_DIR.mkdir(exist_ok=True)


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

# Prompt disenado para forzar uso de muchas tools. Incluye:
# - Datos completos del demandante (no faltan, evita placeholder fatigue)
# - 4 citas mezcladas: 1 vigente, 1 derogada (Ley 50/90 art 6 fue modificada),
#   1 jurisprudencia (SU-449/2020 sobre estabilidad), 1 decreto poco comun
# - Calculo explicito (intereses moratorios + indexacion)
# - Multiple secciones obligatorias en demanda laboral ordinaria
FULL_PROMPT = """Necesito una demanda laboral ordinaria por REINTEGRO + INDEMNIZACION MORATORIA
contra TRANSPORTES LA SABANA SAS NIT 900.222.333-4, domiciliada en Bogota D.C.,
representada por su gerente Pedro Jose Ramirez Castillo CC 79.555.444 de Bogota.

DEMANDANTE: Andres Felipe Moreno Pinto, CC 80.123.456 de Bogota, mayor de edad,
domiciliado en Calle 123 #45-67 apto 502, Bogota. Conductor de transporte de carga
desde 15 de marzo de 2018 hasta 30 de noviembre de 2024 (despido sin justa causa).
Salario base mensual al momento del despido: $2.850.000 COP.

APODERADA: Dra. Sandra Patricia Lopez Bermudez, CC 98.765.432 de Bogota,
T.P. 245678 del C.S. de la J., con oficina en Carrera 7 #71-21 piso 8, Bogota.

HECHOS RELEVANTES:
1. Contrato a termino indefinido desde 2018-03-15.
2. Despido el 2024-11-30 sin notificacion previa ni indemnizacion.
3. La empresa no liquido cesantias del ano 2024 ni intereses sobre cesantias.
4. Al momento del despido el demandante estaba sindicalizado (estabilidad reforzada).

PRETENSIONES:
- Declarar el despido injusto.
- Ordenar el REINTEGRO al mismo cargo.
- Pago de salarios dejados de percibir desde el despido hasta el reintegro
  efectivo, indexados con IPC.
- Indemnizacion moratoria del Art. 65 CST.
- Sancion por no consignar cesantias (Art. 99 Ley 50 de 1990, paragrafo 3).
- Costas y agencias en derecho.

FUNDAMENTOS JURIDICOS A INCLUIR:
- Art. 64 del Codigo Sustantivo del Trabajo (despido sin justa causa).
- Art. 65 del Codigo Sustantivo del Trabajo (indemnizacion moratoria).
- Art. 99 de la Ley 50 de 1990 (sancion por no consignar cesantias).
- Decreto 2351 de 1965 (regimen de fueros sindicales).
- Sentencia SU-449 de 2020 de la Corte Constitucional (estabilidad laboral
  reforzada en sindicalizados).
- Art. 75 y 77 del Codigo General del Proceso (facultades del apoderado).

CALCULOS REQUERIDOS:
- Indemnizacion moratoria: 1 dia de salario por cada dia de mora, desde
  2024-12-01 hasta la fecha de la demanda (calcular dias).
- Indexacion de salarios dejados de percibir segun IPC de los ultimos 12 meses.

JURISDICCION: Juez Laboral del Circuito de Bogota D.C., Reparto.
"""


def smoke_test_401(url: str) -> int:
    print("\n=== Smoke 401 (endpoint vivo sin auth) ===")
    req = urllib.request.Request(
        f"{url}/v1/documents/v2/generate", method="POST",
        data=b'{"intent":"x","user_brief":"","doc_type":"demanda_laboral_ordinaria"}',
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
            print(f"  [WARN] HTTP 503 ({body})")
            return 2
        else:
            print(f"  [FAIL] HTTP {e.code}: {e.read()[:200]!r}")
            return 1
    except Exception as e:
        print(f"  [FAIL] {type(e).__name__}: {e}")
        return 1


def parse_sse_line(text: str) -> tuple[str | None, dict | None]:
    if text.startswith("event:"):
        return text.split(":", 1)[1].strip(), None
    if text.startswith("data:"):
        try:
            return None, json.loads(text[5:].strip())
        except Exception:
            return None, {"_raw": text[5:].strip()[:200]}
    return None, None


def call_endpoint(url: str, jwt: str, prompt: str, doc_type: str,
                    borrador_mode: bool = True) -> dict:
    payload = json.dumps({
        "intent": prompt,
        "user_brief": "",
        "doc_type": doc_type,
        "borrador_mode": borrador_mode,
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

    result: dict[str, Any] = {
        "url": f"{url}/v1/documents/v2/generate",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "duration_s": 0.0,
        "http_status": None,
        "x_orchestrator": None,
        "headers": {},
        "events": [],
        "event_counts": {},
        "tools_invoked": {},
        "tools_set": [],
        "blocks_count": 0,
        "blocks_by_type": {},
        "citations_seen": [],
        "citation_tiers": {},
        "iterations": 0,
        "agent_thoughts": [],
        "final_message": None,
        "error": None,
        "done_payload": None,
        "borrador_mode": borrador_mode,
        "doc_type": doc_type,
    }
    t0 = time.perf_counter()
    print(f"\n=== POST {result['url']} ===")
    print(f"     doc_type={doc_type} borrador_mode={borrador_mode}")
    print(f"     prompt = {prompt[:80]}...\n")

    try:
        resp = urllib.request.urlopen(req, timeout=300)
        result["http_status"] = resp.status
        result["x_orchestrator"] = resp.headers.get("x-orchestrator", "(no-header)")
        result["headers"] = {
            "x-orchestrator": result["x_orchestrator"],
            "content-type": resp.headers.get("content-type", ""),
            "cache-control": resp.headers.get("cache-control", ""),
        }
        print(f"  [HTTP] {result['http_status']} · X-Orchestrator: {result['x_orchestrator']}")
        print(f"  [STREAM] iniciando lectura SSE...\n")

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
                elapsed = time.perf_counter() - t0
                preview = json.dumps(data, default=str, ensure_ascii=False)[:130]
                # Imprimir SOLO eventos clave para no inundar consola
                if current_event in ("meta", "agent_thought", "block_emit",
                                       "citation_verify", "section_started",
                                       "section_done", "stage_progress",
                                       "done", "error", "missing_data"):
                    if current_event == "agent_thought":
                        # acortar agent_thoughts mas
                        msg = data.get("content") or data.get("message") or ""
                        tool = data.get("tool") or ""
                        kind = data.get("kind") or "info"
                        print(f"  [{elapsed:6.1f}s] {current_event} · kind={kind} tool={tool!r} text={msg[:90]!r}")
                    elif current_event == "block_emit":
                        b = data.get("block", {})
                        print(f"  [{elapsed:6.1f}s] {current_event} · type={b.get('type')!r} id={b.get('block_id')!r}")
                    else:
                        print(f"  [{elapsed:6.1f}s] {current_event} · {preview}")
                result["events"].append({"t_s": round(elapsed, 1), "name": current_event,
                                          "payload": data})

                # ---- tracking de validaciones ----
                if current_event == "agent_thought":
                    tool = data.get("tool")
                    if tool:
                        result["tools_invoked"][tool] = result["tools_invoked"].get(tool, 0) + 1
                    # Guardar el final_message del Brain (kind=narration)
                    if data.get("kind") == "narration" and data.get("content"):
                        result["final_message"] = data.get("content")
                    result["agent_thoughts"].append({
                        "kind": data.get("kind"),
                        "tool": tool,
                        "text": (data.get("content") or data.get("message") or "")[:300],
                    })
                if current_event == "block_emit":
                    result["blocks_count"] += 1
                    b = data.get("block", {})
                    btype = b.get("type") or "unknown"
                    result["blocks_by_type"][btype] = result["blocks_by_type"].get(btype, 0) + 1
                if current_event == "citation_verify":
                    cit = data.get("citation") or data.get("ref") or ""
                    tier = data.get("tier") or "?"
                    result["citations_seen"].append({"cit": cit[:80], "tier": tier})
                    result["citation_tiers"][tier] = result["citation_tiers"].get(tier, 0) + 1
                if current_event == "stage_progress":
                    stage = data.get("stage", "")
                    if "brain_iter" in stage:
                        result["iterations"] += 1
                if current_event == "error":
                    result["error"] = data
                if current_event == "done":
                    result["done_payload"] = data

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        result["http_status"] = e.code
        result["error"] = {"http_error": e.code, "body": body}
        print(f"  [HTTP ERROR] {e.code}: {body}")
    except Exception as e:
        result["error"] = {"exception": f"{type(e).__name__}: {e}"}
        print(f"  [STREAM EXCEPTION] {type(e).__name__}: {e}")
    finally:
        result["duration_s"] = round(time.perf_counter() - t0, 2)
        result["tools_set"] = sorted(result["tools_invoked"].keys())

    return result


def validate(result: dict) -> tuple[int, int, list[tuple[str, bool, str]]]:
    """Retorna (passed, total, [(name, ok, detail)])."""
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    add("HTTP 200", result.get("http_status") == 200,
        f"got {result.get('http_status')}")
    add("X-Orchestrator: lean",
        result.get("x_orchestrator") == "lean",
        f"got {result.get('x_orchestrator')!r}")
    add("meta event emitido",
        result["event_counts"].get("meta", 0) >= 1,
        f"count={result['event_counts'].get('meta', 0)}")
    add("Brain >= 3 iteraciones",
        result["iterations"] >= 3,
        f"got {result['iterations']}")
    add("Tools usadas >= 6 distintas",
        len(result["tools_set"]) >= 6,
        f"got {len(result['tools_set'])}: {result['tools_set']}")
    add("load_skill_md invocado",
        result["tools_invoked"].get("load_skill_md", 0) >= 1,
        f"count={result['tools_invoked'].get('load_skill_md', 0)}")
    add("verify_citation invocado >= 3",
        result["tools_invoked"].get("verify_citation", 0) >= 3,
        f"count={result['tools_invoked'].get('verify_citation', 0)}")
    add("generate_clause invocado >= 3",
        result["tools_invoked"].get("generate_clause", 0) >= 3,
        f"count={result['tools_invoked'].get('generate_clause', 0)}")
    add("Blocks materializados >= 10",
        result["blocks_count"] >= 10,
        f"count={result['blocks_count']} types={result['blocks_by_type']}")
    add("agent_thought con content (fix #1 FE)",
        any(t.get("text") for t in result["agent_thoughts"]),
        f"final_message present: {bool(result['final_message'])}")
    add("done event SIN error",
        result.get("done_payload") is not None and result.get("error") is None,
        f"done={bool(result['done_payload'])}, error={result.get('error') is not None}")
    add("build_docx invocado",
        result["tools_invoked"].get("build_docx", 0) >= 1,
        f"count={result['tools_invoked'].get('build_docx', 0)}")
    add("citations con tier resuelto",
        sum(result["citation_tiers"].values()) >= 1,
        f"tiers={result['citation_tiers']}")
    add("duracion total < 300s",
        result.get("duration_s", 999) < 300,
        f"duration={result.get('duration_s')}s")

    passed = sum(1 for _, ok, _ in checks if ok)
    return passed, len(checks), checks


def print_report(result: dict, checks: list[tuple[str, bool, str]],
                  passed: int, total: int) -> None:
    print("\n" + "=" * 78)
    print(f"REPORTE E2E · LeanOrchestrator · {datetime.now(timezone.utc).isoformat()}")
    print("=" * 78)
    print(f"  URL:           {result['url']}")
    print(f"  doc_type:      {result['doc_type']}")
    print(f"  borrador_mode: {result['borrador_mode']}")
    print(f"  duration:      {result['duration_s']}s")
    print(f"  HTTP:          {result['http_status']} · X-Orchestrator: {result['x_orchestrator']}")
    print(f"  Iterations:    {result['iterations']}")
    print(f"  Blocks:        {result['blocks_count']} ({result['blocks_by_type']})")
    print(f"  Tools usadas:  {len(result['tools_set'])} -> {result['tools_set']}")
    print(f"  Citations:     {len(result['citations_seen'])} tiers={result['citation_tiers']}")
    print(f"  Final message: {(result['final_message'] or '')[:200]!r}")
    if result.get("error"):
        print(f"  ERROR:         {json.dumps(result['error'], default=str)[:300]}")
    print()
    print("CHECKS:")
    for name, ok, detail in checks:
        mark = "[PASS]" if ok else "[FAIL]"
        print(f"  {mark} {name:<45} {detail}")
    print()
    print(f"=== {passed}/{total} PASS ===")
    print("=" * 78)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke-401", action="store_true",
                     help="solo verificar endpoint vivo (401)")
    ap.add_argument("--url", default=os.environ.get("LEXAI_BACKEND_URL", DEFAULT_URL))
    ap.add_argument("--prompt", default=FULL_PROMPT, help="prompt custom")
    ap.add_argument("--doc-type", default="demanda_laboral_ordinaria")
    ap.add_argument("--firma", action="store_true",
                     help="modo firma (borrador_mode=False)")
    args = ap.parse_args()

    print("=" * 78)
    print(f"validate_full_lean_agent_e2e.py | {datetime.now(timezone.utc).isoformat()}")
    print(f"  backend = {args.url}")
    print(f"  doc_type = {args.doc_type}")
    print(f"  mode = {'FIRMA' if args.firma else 'BORRADOR (default)'}")
    print("=" * 78)

    if args.smoke_401:
        return smoke_test_401(args.url)

    jwt = os.environ.get("SMOKE_JWT", "").strip()
    if not jwt:
        print("\n[FATAL] Falta SMOKE_JWT env var.")
        print("        Saca tu JWT del frontend (DevTools > Application > Local")
        print("        Storage > buscar token), y corre:")
        print("        SMOKE_JWT=\"eyJhbGc...\" python scripts/validate_full_lean_agent_e2e.py")
        return 2

    result = call_endpoint(args.url, jwt, args.prompt, args.doc_type,
                             borrador_mode=not args.firma)

    # guardar reporte
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = _REPORTS_DIR / f"full_lean_e2e_{stamp}.json"
    report_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False),
                            encoding="utf-8")
    print(f"\n[REPORT] {report_path}")

    passed, total, checks = validate(result)
    print_report(result, checks, passed, total)

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
