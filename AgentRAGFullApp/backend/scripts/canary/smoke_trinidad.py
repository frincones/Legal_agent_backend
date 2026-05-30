"""Sprint M20.05 · S4.3 · Smoke Trinidad real · poder_especial caso clásico.

Genera el mismo poder Trinidad que históricamente se usa de referencia
en el equipo (caso completo con sustitución, facultades amplias,
conciliación). Compara contra el DOCX de referencia que Claude desktop
produjo (si está disponible localmente).

USO:
  python scripts/canary/smoke_trinidad.py <firm_uuid>
  python scripts/canary/smoke_trinidad.py <firm_uuid> --reference <path_docx>
"""
from __future__ import annotations

import argparse
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


TRINIDAD_PROMPT = {
    "doc_type": "poder_especial",
    "intent": (
        "Necesito un poder especial para que mi abogada Dra. Trinidad Castellanos "
        "(CC 52.789.456 de Bogotá, T.P. 123456 del C.S. de la J.) me represente en "
        "el proceso laboral ordinario que cursa en el Juzgado 11 Laboral del Circuito "
        "de Bogotá, contra mi ex-empleador SERVICIOS LOGÍSTICOS DEL CARIBE SAS "
        "(NIT 900.555.444-1), en el que reclamo el reintegro al cargo de Coordinador "
        "de Operaciones desempeñado, el pago de salarios caídos desde el despido "
        "(15 de enero de 2025), las prestaciones sociales causadas y los aportes a "
        "seguridad social. Poderdante: Andrés Felipe Moreno Pinto, CC 80.123.456 "
        "de Bogotá. Con facultad expresa de SUSTITUIR el poder en otro abogado titulado, "
        "CONCILIAR las pretensiones en cualquier audiencia o oportunidad procesal, "
        "DESISTIR total o parcialmente, y RECIBIR cualquier suma de dinero que en "
        "virtud del proceso me sea reconocida. Vigencia: hasta la ejecutoria de la "
        "sentencia que ponga fin al proceso, incluidos los recursos extraordinarios."
    ),
    "brief": "",
}


def run_smoke(url: str, token: str, firm_id: str) -> dict:
    import urllib.request

    payload = json.dumps({
        "intent": TRINIDAD_PROMPT["intent"],
        "user_brief": TRINIDAD_PROMPT["brief"],
        "doc_type": TRINIDAD_PROMPT["doc_type"],
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
        "duration_s": 0,
        "status_code": None,
        "x_orchestrator": None,
        "events": [],
        "full_text": [],
        "citations": [],
        "error": None,
    }
    try:
        with urllib.request.urlopen(req, timeout=240) as resp:
            result["status_code"] = resp.status
            result["x_orchestrator"] = resp.headers.get("X-Orchestrator")
            payload_buf = ""
            current_event = None
            for line in resp:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text.startswith("event:"):
                    current_event = text.split(":", 1)[1].strip()
                    result["events"].append(current_event)
                    payload_buf = ""
                elif text.startswith("data:"):
                    payload_buf = text[5:].strip()
                    try:
                        data = json.loads(payload_buf)
                    except Exception:
                        data = {}
                    if current_event == "block_emit":
                        block = data.get("block", {})
                        for r in block.get("runs", []) or []:
                            if isinstance(r, dict) and "text" in r:
                                result["full_text"].append(r["text"])
                        for k in ("text", "ratio", "contenido"):
                            if isinstance(block.get(k), str):
                                result["full_text"].append(block[k])
                    elif current_event == "citation_verify":
                        result["citations"].append({
                            "citation": data.get("citation"),
                            "tier": data.get("tier"),
                            "found": data.get("found"),
                            "fuente_url": data.get("fuente_url_oficial"),
                        })
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    result["duration_s"] = round(time.perf_counter() - t0, 2)
    return result


def render(result: dict, reference_path: Path | None) -> int:
    print(f"\n=== Trinidad smoke ===")
    print(f"  status:        {result['status_code']}")
    print(f"  orchestrator:  {result['x_orchestrator']}")
    print(f"  duration:      {result['duration_s']}s")
    print(f"  events:        {len(result['events'])}")
    print(f"  text length:   {sum(len(t) for t in result['full_text'])} chars")
    print(f"  citations:     {len(result['citations'])}")
    if result["citations"]:
        print(f"\n  Citas verificadas:")
        for c in result["citations"]:
            tier_icon = {"GROUNDED": "✓", "DEROGADA": "✗", "VERIFY_FLAG": "⚠", "NOT_FOUND": "?"}.get(c.get("tier"), "·")
            print(f"    {tier_icon} {c['citation']} → {c['tier']} ({c['fuente_url'] or 'no_url'})")
    if result.get("error"):
        print(f"\n  ERROR: {result['error']}")
        return 1

    # Save output
    out_dir = _BACKEND_ROOT / "reports" / "trinidad"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"trinidad_smoke_{ts}.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  → guardado: {out_path}")

    # Reference comparison (heurística simple si hay path)
    if reference_path and reference_path.exists():
        print(f"\n  Comparando con referencia: {reference_path}")
        try:
            from docx import Document   # type: ignore
            doc = Document(str(reference_path))
            ref_text = "\n".join(p.text for p in doc.paragraphs)
            generated_text = " ".join(result["full_text"])
            common_words = set(ref_text.split()) & set(generated_text.split())
            print(f"    Overlap palabras: {len(common_words)} comunes")
            print(f"    Sugerencia: para comparación profunda, correr llm_judge sobre ambos textos")
        except ImportError:
            print(f"    (python-docx no instalado; instalar para comparar)")
        except Exception as e:
            print(f"    WARN comparación falló: {e}")

    print(f"\n=== {'OK' if 'done' in result['events'] else 'FAIL'} ===")
    return 0 if "done" in result["events"] else 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("firm_uuid")
    p.add_argument("--url", default=os.getenv("LEXAI_BACKEND_URL", DEFAULT_URL))
    p.add_argument("--token", default=os.getenv("SMOKE_JWT", ""))
    p.add_argument("--reference", type=Path, default=None,
                    help="Path opcional al DOCX de referencia (e.g., Claude desktop output)")
    args = p.parse_args()

    result = run_smoke(args.url, args.token, args.firm_uuid)
    return render(result, args.reference)


if __name__ == "__main__":
    sys.exit(main())
