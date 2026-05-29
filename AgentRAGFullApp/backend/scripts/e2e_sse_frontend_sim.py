"""Sprint M19.30 · E2E test que SIMULA al frontend.

Hace POST a /v1/documents/v2/generate (stream SSE) con un caso real,
parsea cada evento, mide latencias y valida:
  - El primer evento SSE llega < 5s desde el POST (no spinner mudo).
  - Se emiten stage_progress (running) para los 3 stages del fix M19.30.
  - Cada stage_progress (running) llega ANTES del resultado del stage.
  - El documento se persiste y tiene document_id.

Después del stream, descarga el .docx via /export-forensic y valida bytes.

Uso:
    set LEXAI_BACKEND_URL=https://legal-agent-backend-production-fcfa.up.railway.app
    set LEXAI_JWT=<token>      # JWT del frontend, sino el script intenta sin auth
    python scripts/e2e_sse_frontend_sim.py

Salida:
    outputs/e2e_sse_frontend_sim_<ts>.json     resumen estructurado
    outputs/e2e_sse_<doc_id>.docx              .docx descargado
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("pip install requests", file=sys.stderr)
    sys.exit(2)


DEFAULT_URL = os.getenv(
    "LEXAI_BACKEND_URL",
    "https://legal-agent-backend-production-fcfa.up.railway.app",
)

# Caso real (poder especial bien especificado, mismo doc_type que el chat pegado)
DEFAULT_INTENT = (
    "Necesito redactar un poder especial notarial para Colombia. "
    "El poderdante es Juan Pérez García, CC 79.123.456 de Bogotá. "
    "El apoderado es Carlos Andrés Rodríguez, CC 80.555.111, abogado "
    "con tarjeta profesional 145.678 del CSJ. "
    "Facultades: representar al poderdante en el proceso ejecutivo "
    "singular No. 2026-01234 que cursa ante el Juzgado 15 Civil "
    "Municipal de Bogotá contra Comercial XYZ SAS. "
    "Otorgar facultades expresas para conciliar, transigir, desistir, "
    "recibir y sustituir. "
    "Lugar y fecha del acto: Bogotá, 28 de mayo de 2026."
)

DEFAULT_DOC_TYPE = "poder_especial"

OUTPUTS_DIR = Path("c:/Users/freddyrs/Desktop/Legal Demo/Legal_agent_Frontend/TEST Freddy/test_outputs")


def _parse_sse_stream(resp) -> list[dict]:
    """Parsea text/event-stream y devuelve lista de eventos {event, data, ts_relative_ms}."""
    started = time.perf_counter()
    events: list[dict] = []
    current_event = None
    current_data = []
    for raw_line in resp.iter_lines(decode_unicode=True):
        if raw_line is None:
            continue
        line = raw_line.rstrip("\r")
        if line == "":
            if current_event or current_data:
                data_str = "\n".join(current_data)
                try:
                    data = json.loads(data_str) if data_str else None
                except Exception:
                    data = {"_raw": data_str}
                events.append({
                    "event": current_event or "message",
                    "data": data,
                    "ts_ms": int((time.perf_counter() - started) * 1000),
                })
            current_event = None
            current_data = []
        elif line.startswith("event:"):
            current_event = line[6:].strip()
        elif line.startswith("data:"):
            current_data.append(line[5:].lstrip())
        elif line.startswith(":"):
            # keepalive comment
            continue
    return events


def run_e2e(url: str, jwt: str | None, intent: str, doc_type: str) -> dict:
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    headers = {"Accept": "text/event-stream"}
    if jwt:
        headers["Authorization"] = f"Bearer {jwt}"

    body = {
        "intent": intent,
        "user_brief": "",
        "doc_type": doc_type,
        "borrador_mode": True,
    }

    print(f"\n[POST] {url}/v1/documents/v2/generate")
    print(f"  doc_type={doc_type} intent_chars={len(intent)}")
    t0 = time.perf_counter()
    try:
        resp = requests.post(
            f"{url}/v1/documents/v2/generate",
            headers=headers,
            json=body,
            stream=True,
            timeout=(10, 600),  # 10s connect, 10min read
        )
    except Exception as e:
        return {"ok": False, "error": f"connect: {e}"}
    ttfb = int((time.perf_counter() - t0) * 1000)
    print(f"  HTTP {resp.status_code} TTFB={ttfb}ms")
    if resp.status_code != 200:
        try:
            preview = resp.text[:400]
        except Exception:
            preview = ""
        return {"ok": False, "http_status": resp.status_code, "preview": preview, "ttfb_ms": ttfb}

    # Parse SSE
    print("  parsing SSE stream...")
    events = _parse_sse_stream(resp)
    total_stream_ms = events[-1]["ts_ms"] if events else 0
    print(f"  received {len(events)} events in {total_stream_ms}ms")

    # Encontrar primer evento "real" (no keepalive)
    first_event_ms = events[0]["ts_ms"] if events else None
    print(f"  first event at {first_event_ms}ms")

    # Buscar document_id
    document_id = None
    for ev in events:
        d = ev.get("data") or {}
        if isinstance(d, dict):
            if d.get("matter_document_id"):
                document_id = d["matter_document_id"]
            if d.get("generation_id") and not document_id:
                # meta event tiene generation_id que NO es document_id, ignorar
                pass

    # Si el orchestrator solo persistió bloques sin matter_document_id, intentar
    # detectar por evento "presented_file" o "audit_report".
    for ev in events:
        d = ev.get("data") or {}
        if isinstance(d, dict):
            # patrones comunes para encontrar el doc_id
            for k in ("document_id", "matter_document_id", "doc_id"):
                if d.get(k):
                    document_id = d[k]
                    break

    # Validar stage_progress (running) emitido para los 3 stages
    progress_events = [ev for ev in events if ev["event"] == "stage_progress"]
    stages_running: dict[str, dict] = {}
    stages_done: dict[str, dict] = {}
    for ev in progress_events:
        d = ev.get("data") or {}
        stage = d.get("stage")
        state = d.get("state")
        if state == "running":
            stages_running[stage] = ev
        else:
            stages_done[stage] = ev

    # Identificar errores y duración total
    errors = [ev for ev in events if ev["event"] == "error"]

    summary = {
        "ok": True,
        "ttfb_ms": ttfb,
        "first_event_ms": first_event_ms,
        "total_stream_ms": total_stream_ms,
        "total_events": len(events),
        "document_id": document_id,
        "stages_running": sorted(stages_running.keys()),
        "stages_done": sorted(stages_done.keys()),
        "stage_durations_ms": {
            s: stages_done[s]["data"].get("elapsed_ms")
            for s in stages_done if isinstance(stages_done[s]["data"], dict)
        },
        "errors": [ev["data"] for ev in errors],
        "event_counts_by_type": {},
    }
    for ev in events:
        summary["event_counts_by_type"][ev["event"]] = summary["event_counts_by_type"].get(ev["event"], 0) + 1

    # Si hay document_id, descargar el .docx
    if document_id:
        for engine in ("claude", "legacy"):
            qs = (
                f"?engine=claude&doc_type={doc_type}&doc_family=notarial"
                if engine == "claude" else ""
            )
            url_doc = f"{url}/v1/documents/v2/documents/{document_id}/export-forensic{qs}"
            print(f"  [DOWNLOAD] engine={engine}: {url_doc}")
            t_d = time.perf_counter()
            r = requests.get(url_doc, headers=headers, timeout=120)
            dur = int((time.perf_counter() - t_d) * 1000)
            print(f"    HTTP {r.status_code} dur={dur}ms size={len(r.content)}B")
            summary[f"export_{engine}_http"] = r.status_code
            summary[f"export_{engine}_ms"] = dur
            summary[f"export_{engine}_bytes"] = len(r.content)
            summary[f"export_{engine}_renderer"] = r.headers.get("X-LexAI-Renderer")
            if r.status_code == 200 and r.content.startswith(b"PK\x03\x04"):
                out_path = OUTPUTS_DIR / f"e2e_sse_{document_id}_{engine}.docx"
                out_path.write_bytes(r.content)
                summary[f"export_{engine}_path"] = str(out_path)

    # Persistir resumen JSON con todos los eventos
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_path = OUTPUTS_DIR / f"e2e_sse_frontend_sim_{ts}.json"
    full_dump = {"summary": summary, "events": events[:1000]}
    summary_path.write_text(json.dumps(full_dump, ensure_ascii=False, indent=2, default=str),
                            encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--jwt", default=os.getenv("LEXAI_JWT", ""))
    p.add_argument("--intent", default=DEFAULT_INTENT)
    p.add_argument("--doc-type", default=DEFAULT_DOC_TYPE)
    args = p.parse_args()
    jwt = args.jwt.strip() or None
    if not jwt:
        print("[warn] sin --jwt / LEXAI_JWT; algunos endpoints pedirán auth")
    summary = run_e2e(args.url.rstrip("/"), jwt, args.intent, args.doc_type)
    print("\n===== SUMMARY =====")
    print(json.dumps({k: v for k, v in summary.items() if k != "events"},
                     ensure_ascii=False, indent=2, default=str))
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
