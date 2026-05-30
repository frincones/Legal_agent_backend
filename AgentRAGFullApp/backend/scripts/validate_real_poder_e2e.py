"""Sprint M20 · validate_real_poder_e2e.py · validación REAL del agente.

Ejecuta el LeanOrchestrator REAL (Brain Anthropic + OpenAI + tools de verdad)
contra un prompt complejo de poder especial para notaría con MÚLTIPLES citas
legales que ejercitan: verify_citation (4-tier), generate_clause, check_coherence,
build_docx, persist_audit.

NO requiere pool Supabase funcional (degradación graceful via heurística).
Requiere ANTHROPIC_API_KEY + OPENAI_API_KEY en env.

USO:
    ANTHROPIC_API_KEY=sk-ant-... python scripts/validate_real_poder_e2e.py

Output:
  - SSE event log completo (stdout)
  - reports/real_poder_e2e_YYYYMMDD_HHMMSS.json  (eventos + métricas)
  - reports/real_poder_e2e_YYYYMMDD_HHMMSS.docx  (si build_docx tuvo éxito)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

_BACKEND_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_BACKEND_ROOT))


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(_BACKEND_ROOT / ".env")
_load_env(Path(r"C:\Users\freddyrs\Desktop\Legal Demo\Legal_agent_Frontend\.env.local"))


# ============================================================
# PROMPT COMPLEJO · ejercita TODO el agente
# ============================================================

COMPLEX_PROMPT = """Necesito un PODER ESPECIAL para ser otorgado ante notaría
en Bogotá, con TODOS los requisitos formales del Estatuto del Notariado
(Decreto 960 de 1970) y del régimen general del mandato (Arts. 2142 y ss. del
Código Civil).

DATOS DEL PODERDANTE:
  Nombre: Carlos Alberto Rincones Pérez
  Cédula: 80.123.456 expedida en Bogotá
  Estado civil: casado
  Profesión: comerciante
  Domicilio: Calle 100 # 11-50 apto 502, Bogotá D.C.

DATOS DE LA APODERADA:
  Nombre: Dra. Sandra Marcela López Mendoza
  Cédula: 52.789.456 expedida en Bogotá
  Tarjeta Profesional: 123456 del Consejo Superior de la Judicatura
  Inscrita ante: Colegio de Abogados Litigantes

OBJETO DEL PODER:
  Representación judicial AMPLIA en el proceso ejecutivo singular
  promovido por mi mandante INVERSIONES TRINIDAD SAS NIT 900.111.222-3
  contra CONSTRUCTORA ANDINA LTDA por la suma de $354.166.667 más
  intereses moratorios DTF + 1% mensual desde el 31 de diciembre de 2024
  hasta el pago efectivo, con base en el contrato de obra incumplido.

FACULTADES EXPRESAS REQUERIDAS:
  a) SUSTITUIR el presente poder en abogado titulado e inscrito
     (con base en el Art. 75 del Código General del Proceso).
  b) CONCILIAR, transigir y desistir total o parcialmente (Art. 77 CGP).
  c) RECIBIR pagos derivados del proceso.
  d) INTERPONER todos los recursos ordinarios y extraordinarios,
     incluido el recurso de casación si fuere procedente.
  e) Solicitar MEDIDAS CAUTELARES (embargo, secuestro) conforme a
     los Arts. 590 y ss. del CGP.

CITAS LEGALES OBLIGATORIAS a incluir y verificar:
  - Arts. 2142, 2143, 2156, 2189 del Código Civil (régimen del mandato)
  - Art. 75 y Art. 77 del Código General del Proceso
  - Art. 836 del Código de Comercio (NOTA: VERIFICAR si existe o sugerir
    alternativa; el demandado es sociedad comercial)
  - Decreto 960 de 1970 (Estatuto Notarial)
  - Arts. 588 y 590 del CGP (medidas cautelares)
  - Sentencia SC2879-2019 de la Sala Civil de la Corte Suprema
    sobre poderes con facultades de transacción y conciliación.

VIGENCIA: hasta la EJECUTORIA de la sentencia que ponga fin al proceso,
incluidos los recursos extraordinarios y, en su caso, la fase de
cumplimiento de la sentencia.

REQUISITOS NOTARIALES:
  - Encabezado dirigido al notario que dará fe.
  - Cláusula de presentación personal.
  - Espacios para firma del poderdante y aceptación de la apoderada.
  - Diligencia notarial al final.

Por favor genera el documento completo, formal, listo para imprimir y
presentar ante notario, con todas las citas verificadas e identificando
explícitamente cualquier cita que no exista o esté derogada.
"""


# ============================================================
# Runner
# ============================================================

class EventCapture:
    def __init__(self):
        self.events: list[dict] = []
        self.t0 = time.perf_counter()

    def add(self, name: str, payload: dict) -> None:
        self.events.append({
            "t_ms": int((time.perf_counter() - self.t0) * 1000),
            "name": name,
            "payload": payload,
        })

    def count(self, name: str) -> int:
        return sum(1 for e in self.events if e["name"] == name)

    def by_name(self, name: str) -> list[dict]:
        return [e for e in self.events if e["name"] == name]

    def names(self) -> list[str]:
        return [e["name"] for e in self.events]


def parse_sse(data: bytes) -> tuple[str | None, dict]:
    text = data.decode("utf-8", errors="replace")
    if not text.startswith("event:"):
        return None, {}
    lines = text.strip().split("\n")
    name = lines[0].split(":", 1)[1].strip()
    payload = {}
    for ln in lines[1:]:
        if ln.startswith("data:"):
            try:
                payload = json.loads(ln[5:].strip())
            except Exception:
                payload = {}
    return name, payload


async def run() -> int:
    print("=" * 75)
    print(f"validate_real_poder_e2e.py · {datetime.now(timezone.utc).isoformat()}")
    print("=" * 75)

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("\n[FAIL] ANTHROPIC_API_KEY no configurado.")
        print("Uso: ANTHROPIC_API_KEY=sk-ant-... python scripts/validate_real_poder_e2e.py")
        return 1
    if not os.getenv("OPENAI_API_KEY"):
        print("\n[FAIL] OPENAI_API_KEY no configurado en .env")
        return 1

    # Imports después de cargar env
    from uuid import uuid4
    from lex.orchestrator.lean_orchestrator import LeanOrchestrator
    from utils.llm_provider import _get_anthropic_client
    from utils.llm import get_openai_client

    anth = _get_anthropic_client()
    openai = get_openai_client()
    if anth is None:
        print("[FAIL] Anthropic client no se pudo inicializar.")
        return 1
    print(f"\n[setup] Anthropic client: OK")
    print(f"[setup] OpenAI client:    {'OK' if openai else 'FAIL'}")
    print(f"[setup] Pool Supabase:    None (degradación graceful)")

    generation_id = uuid4()
    firm_id = uuid4()
    user_id = uuid4()
    print(f"[setup] generation_id: {generation_id}")
    print(f"[setup] firm_id:       {firm_id}")
    print(f"[setup] prompt length: {len(COMPLEX_PROMPT)} chars\n")

    orchestrator = LeanOrchestrator(
        anthropic_client=anth,
        openai_client=openai,
        pool=None,
        firm_id=firm_id,
        user_id=user_id,
        generation_id=generation_id,
    )

    capture = EventCapture()
    print("=" * 75)
    print("EJECUTANDO ReAct LOOP REAL (Brain Anthropic Sonnet 4.6)")
    print("=" * 75)

    try:
        async for sse_bytes in orchestrator.run(
            intent=COMPLEX_PROMPT,
            brief="",
            doc_type_hint="poder_especial",
        ):
            name, payload = parse_sse(sse_bytes)
            if not name:
                continue
            capture.add(name, payload)
            # Live output (resumido)
            if name == "meta":
                print(f"[{capture.events[-1]['t_ms']/1000:5.1f}s] meta · model={payload.get('model')} · "
                      f"caching={payload.get('caching_enabled')} · tools={payload.get('tools_count')}")
            elif name == "stage_progress":
                print(f"[{capture.events[-1]['t_ms']/1000:5.1f}s] stage_progress · {payload.get('label', '')}")
            elif name == "agent_thought":
                tool = payload.get("tool", "")
                content = (payload.get("content", "") or "")[:90]
                print(f"[{capture.events[-1]['t_ms']/1000:5.1f}s] agent_thought [{tool}] · {content}")
            elif name == "citation_verify":
                print(f"[{capture.events[-1]['t_ms']/1000:5.1f}s] citation_verify · "
                      f"{payload.get('citation', '')} -> found={payload.get('found')}")
            elif name == "section_started":
                print(f"[{capture.events[-1]['t_ms']/1000:5.1f}s] section_started · {payload.get('section_key')}")
            elif name == "block_emit":
                print(f"[{capture.events[-1]['t_ms']/1000:5.1f}s] block_emit · {payload.get('section_key')}")
            elif name == "section_done":
                print(f"[{capture.events[-1]['t_ms']/1000:5.1f}s] section_done · {payload.get('section_key')}")
            elif name == "done":
                print(f"[{capture.events[-1]['t_ms']/1000:5.1f}s] [DONE] tokens_in={payload.get('tokens_input')} · "
                      f"tokens_out={payload.get('tokens_output')} · iter={payload.get('iterations')}")
            elif name == "error":
                print(f"[{capture.events[-1]['t_ms']/1000:5.1f}s] [ERROR] {payload}")
    except Exception as e:
        print(f"\n[EXCEPTION] {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 1

    # ============================================================
    # Análisis post-ejecución
    # ============================================================
    print("\n" + "=" * 75)
    print("ANÁLISIS · ¿todas las funcionalidades probadas?")
    print("=" * 75)

    checks = []

    def add_check(name: str, condition: bool, detail: str = "") -> None:
        icon = "[OK]" if condition else "[FAIL]"
        print(f"  {icon} {name}{' · ' + detail if detail else ''}")
        checks.append((name, condition, detail))

    # 1. Lifecycle
    add_check("meta event emitido", capture.count("meta") >= 1)
    add_check("done event emitido (sin error)",
              capture.count("done") >= 1 and capture.count("error") == 0)

    # 2. Tools invocadas (vía agent_thought)
    tools_invoked = set()
    for e in capture.by_name("agent_thought"):
        tool = e["payload"].get("tool")
        if tool:
            tools_invoked.add(tool)
    print(f"\n  Tools invocadas por el Brain: {sorted(tools_invoked)}")

    add_check("Brain invocó verify_citation",
              "verify_citation" in tools_invoked,
              f"({sum(1 for e in capture.by_name('agent_thought') if e['payload'].get('tool') == 'verify_citation')} veces)")
    add_check("Brain generó al menos 1 cláusula",
              "generate_clause" in tools_invoked or capture.count("block_emit") > 0)

    # 3. Citas verificadas
    citation_events = capture.by_name("citation_verify")
    add_check(f"≥ 3 citas verificadas", len(citation_events) >= 3,
              f"({len(citation_events)} citas)")

    # 4. Bloques generados
    blocks_emitted = capture.count("block_emit")
    add_check(f"≥ 3 bloques emitidos", blocks_emitted >= 3,
              f"({blocks_emitted} bloques)")

    # 5. Iteraciones del ReAct loop (vía stage_progress)
    iterations = sum(1 for e in capture.by_name("stage_progress")
                     if "brain_iter" in e["payload"].get("stage", ""))
    add_check(f"≥ 2 iteraciones ReAct", iterations >= 2, f"({iterations} iteraciones)")

    # 6. Eventos SSE únicos
    unique_events = set(capture.names())
    add_check(f"≥ 5 tipos de evento SSE distintos",
              len(unique_events) >= 5,
              f"({len(unique_events)} tipos)")

    # 7. Tiers de citas (4-tier system) — extraer del content del agent_thought
    tiers_seen = set()
    for e in citation_events:
        tier = e["payload"].get("tier")
        if tier:
            tiers_seen.add(tier)
    # Buscar tier en el content del agent_thought (formato "Cita 'X': TIER")
    import re as _re
    for e in capture.by_name("agent_thought"):
        if e["payload"].get("tool") == "verify_citation":
            content = e["payload"].get("content", "") or ""
            m = _re.search(r"\b(GROUNDED|VERIFY_FLAG|DEROGADA|NOT_FOUND|MODULADA)\b", content)
            if m:
                tiers_seen.add(m.group(1))
            resp = e["payload"].get("tool_response")
            if isinstance(resp, dict) and resp.get("tier"):
                tiers_seen.add(resp["tier"])

    if tiers_seen:
        add_check(f"4-tier system activo", True, f"tiers vistos: {sorted(tiers_seen)}")
    else:
        add_check("4-tier system (citas con tier explícito)", False,
                  "no se vieron tiers explícitos en payloads (revisar sse_emitter)")

    # 8. final message
    final_msgs = [e for e in capture.by_name("agent_thought")
                  if e["payload"].get("type") == "final_message"]
    add_check("Brain emitió mensaje final al usuario",
              len(final_msgs) >= 1)

    # ============================================================
    # Persistir reporte
    # ============================================================
    reports_dir = _BACKEND_ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_path = reports_dir / f"real_poder_e2e_{ts}.json"
    report = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "generation_id": str(generation_id),
        "firm_id": str(firm_id),
        "prompt_length": len(COMPLEX_PROMPT),
        "total_events": len(capture.events),
        "tools_invoked": sorted(tools_invoked),
        "tiers_seen": sorted(tiers_seen),
        "iterations": iterations,
        "blocks_emitted": blocks_emitted,
        "citations_count": len(citation_events),
        "unique_event_types": sorted(unique_events),
        "checks": [
            {"name": n, "passed": c, "detail": d} for n, c, d in checks
        ],
        "events": capture.events,
    }
    report_path.write_text(json.dumps(report, indent=2, default=str, ensure_ascii=False),
                            encoding="utf-8")
    print(f"\n  [reporte] {report_path}")

    # ============================================================
    # Veredicto
    # ============================================================
    passed = sum(1 for _, c, _ in checks if c)
    total = len(checks)
    print("\n" + "=" * 75)
    if passed == total:
        print(f"[100% PASS] {passed}/{total} validaciones · agente E2E real OK.")
    elif passed >= total * 0.8:
        print(f"[OK con observaciones] {passed}/{total} validaciones pasaron.")
    else:
        print(f"[FAIL] solo {passed}/{total} validaciones pasaron · revisar reporte.")
    print("=" * 75)
    return 0 if passed >= total * 0.8 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
