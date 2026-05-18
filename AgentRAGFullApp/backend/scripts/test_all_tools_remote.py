"""Remote test runner · executes 117 tools against the LIVE Railway backend.

Iterates scripts/tools_catalog.json and for each tool:
  1. Builds mock args using known fixtures (matter_id, document_id, etc.)
  2. POSTs to /v1/admin/tools/test/{tool_name} on the production backend
  3. Captures result · classifies as PASS / SOFT_FAIL / HARD_FAIL / SKIP
  4. Writes CSV + JSON report
  5. Optionally STOPS at first failure of each category so you can fix + retry

Usage:
  python -m scripts.test_all_tools_remote \\
      --api https://legal-agent-backend-production-fcfa.up.railway.app \\
      --token <SUPABASE_JWT> \\
      [--matter-id <UUID>] [--document-id <UUID>] \\
      [--only <name>] [--skip-safety EXTERNAL_SEND,UI_BRIDGE]
      [--stop-on-fail]

JWT: easiest to obtain by logging into the deployed app and copying
`sb-osyrwsbruydcyhdjvjpv-auth-token.0` cookie OR opening DevTools
Network → any /api/* call → request Authorization header.

The remote endpoint enforces admin role · set LEXAI_TOOL_TEST_ALLOW_ALL=true
in the Railway env vars to bypass for staging if you're not yet admin.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
CATALOG_PATH = HERE / "tools_catalog.json"
RESULTS_DIR = HERE.parent / "test-results"


# Per-tool argument overrides · learned from real tool error messages.
# When a tool needs args that don't match generic mock names, list them here.
# Empty dict {} means "send no args" · the tool will error gracefully.
TOOL_ARG_OVERRIDES: dict[str, dict[str, Any]] = {
    # Calculators · Pydantic schemas demand exact field names + valid enums
    "calc_liquidacion": {
        "fecha_ingreso": "2024-01-15", "fecha_terminacion": "2026-03-20",
        "salario_mensual_cop": 2000000, "causa": "injustificado",
        "tipo_contrato": "indefinido",
    },
    "calc_prescripcion": {
        "fecha_exigibilidad": "2024-01-15", "tipo_accion": "civil_ordinaria",
    },
    "calc_intereses": {
        "capital_cop": 5000000, "fecha_inicio": "2024-01-15",
        "fecha_fin": "2026-05-17", "tipo_interes": "comercial_moratorio",
        "metodo": "simple",
    },
    # Norms / citations
    "validate_norm_vigencia": {"tipo": "Ley", "numero": "1564", "anio": 2012},
    "validate_citation": {"citation_ref": "Ley 1564 de 2012 art. 82"},
    "research_jurisprudence": {"query": "despido injustificado"},
    # Documents
    "summarize_document": {"document_id": "{document_id}"},  # placeholder replaced below
    "extract_document_entities": {"document_id": "{document_id}"},
    "ask_about_document": {"document_id": "{document_id}", "question": "¿partes y cuantía?"},
    "analyze_contract": {"document_id": "{document_id}"},
    "check_doc_consistency": {
        "matter_document_id": "{document_id}",
        "document_text": "Documento de prueba con fecha 15 de enero de 2024 y monto $5.000.000.",
    },
    "score_evidence": {
        "matter_document_id": "{document_id}",
        "document_text": "Contrato firmado el 15-01-2024 por las partes Juan Pérez y EPS Demo · valor $2.000.000.",
    },
    "review_contract": {"document_id": "{document_id}"},
    "get_document_content": {"document_id": "{document_id}"},
    "compare_documents": {"document_a_id": "{document_id}", "document_b_id": "{document_id}"},
    "execute_skill": {"command": "/redactar/tutela"},
    "reject_redline": {"redline_set_id": "00000000-0000-0000-0000-000000000000",
                        "redline_id": "00000000-0000-0000-0000-000000000000"},
    "import_csv": {"job_id": "00000000-0000-0000-0000-000000000000"},
    # Time / expenses / billing
    "track_time": {"matter_id": "{matter_id}", "minutes": 60, "description": "test"},
    "log_expense": {"matter_id": "{matter_id}", "amount_cop": 50000, "description": "test"},
    "generate_invoice": {"matter_id": "{matter_id}", "period_start": "2026-05-01", "period_end": "2026-05-31"},
    # Trust
    "record_trust_deposit": {"trust_account_id": "00000000-0000-0000-0000-000000000000",
                              "amount_cop": 100000, "description": "test deposit"},
    "record_trust_payment": {"trust_account_id": "00000000-0000-0000-0000-000000000000",
                              "amount_cop": 50000, "description": "test payment"},
    # Drafts
    "draft_pleading": {"kind": "tutela", "facts": {"accionante": "Test", "accionado": "EPS"}, "matter_id": "{matter_id}"},
    "autofill_template": {"template_body": "Hola {{nombre}}", "matter_id": "{matter_id}"},
    "extract_variables_from_text": {"text": "Juan Pérez 12345", "variables": [{"name": "nombre", "kind": "text"}]},
    # Notes / deadlines / tasks
    "add_matter_note": {"matter_id": "{matter_id}", "body": "test note"},
    "add_matter_deadline": {"matter_id": "{matter_id}", "titulo": "test plazo",
                             "fecha": "2026-12-31", "tipo": "audiencia"},
    "mark_deadline_done": {"deadline_id": "da37c7e1-ee36-4005-8fb1-4e4934cbaa0d"},
    "create_task": {"title": "test task"},
    "complete_task": {"task_id": "e20d7d7e-44f7-4012-b54e-c42e9189d88b"},
    "resolve_comment": {"comment_id": "ef0f4970-6441-4abe-9234-34fd0c7edd64"},
    # Judges + simulations
    "get_judge_stats": {"judge_id": "84ce650c-3602-4d54-8fdf-6953aadba984"},
    "simulate_judge_view": {"matter_id": "{matter_id}", "judge_id": "84ce650c-3602-4d54-8fdf-6953aadba984"},
    # Trust + automation + signatures + import + redline · with real IDs
    "record_trust_deposit": {"trust_account_id": "3dab9e79-5440-4233-af1e-738b53712848",
                              "amount_cop": 50000, "description": "test"},
    "record_trust_payment": {"trust_account_id": "3dab9e79-5440-4233-af1e-738b53712848",
                              "amount_cop": 50000, "description": "test"},
    "run_automation": {"rule_id": "b46cd027-96ef-4dc4-bccf-f29af71fb76e"},
    "check_signature_status": {"envelope_id": "e03af8b9-0b1e-44f7-a604-ddf5900997dd"},
    "import_csv": {"job_id": "489a99fc-8a58-4c4d-b510-ec3684d4edd9"},
    "wizard_session_status": {"session_token": "00000000-0000-0000-0000-000000000000"},
    # Documents · ensure doc text is >100 chars for check_doc_consistency
    "analyze_contract": {"document_id": "{document_id}",
                          "document_text": "Contrato de prueba con cláusulas extensas para superar el umbral mínimo del validador. " * 3},
    # Search / external
    "search_suin_juriscol": {"tipo": "Ley", "numero": "1564", "anio": 2012},
    "verify_rue_persona": {"query": "Juan Perez"},
    "fetch_dof_co_publicacion": {"query": "decreto 2024"},
    "delegate_to": {"subagent": "investigador", "task": "test"},
    # Memory
    "remember": {"key": "test_key", "value": "test_value", "scope": "user"},
    # Wizards
    "list_wizards": {},
    "start_wizard": {"slug": "tutela_salud"},
    # Audit
    "query_audit_logs": {"query": "matter", "limit": 5},
}


def build_mock_args(
    required: list[str],
    *,
    matter_id: Optional[str],
    document_id: Optional[str],
    tool_name: Optional[str] = None,
) -> dict[str, Any]:
    """Build mock args · tool-specific override has priority over generic mock."""
    # If we have a tool-specific override, use it.
    if tool_name and tool_name in TOOL_ARG_OVERRIDES:
        override = TOOL_ARG_OVERRIDES[tool_name]
        out: dict[str, Any] = {}
        for k, v in override.items():
            # Substitute {matter_id} / {document_id} placeholders.
            if isinstance(v, str) and v == "{matter_id}":
                out[k] = matter_id or "00000000-0000-0000-0000-000000000000"
            elif isinstance(v, str) and v == "{document_id}":
                out[k] = document_id or "00000000-0000-0000-0000-000000000000"
            else:
                out[k] = v
        return out
    # Fallback to generic mocks from required_args list.
    out = {}
    for arg in required:
        out[arg] = _mock_for(arg, matter_id=matter_id, document_id=document_id)
    return out


def _mock_for(arg: str, *, matter_id: Optional[str], document_id: Optional[str]) -> Any:
    a = arg.lower()
    placeholder_uuid = "00000000-0000-0000-0000-000000000000"
    if a in ("matter_id",):
        return matter_id or placeholder_uuid
    if a in ("matter_document_id", "document_id", "document_a_id", "document_b_id"):
        return document_id or placeholder_uuid
    if a in (
        "deadline_id", "task_id", "comment_id", "redline_id", "redline_set_id",
        "envelope_id", "trust_account_id", "judge_id", "subject_id_value",
        "rule_id", "firm_id",
    ):
        return placeholder_uuid
    if a in ("days", "months"): return 30
    if a == "hours": return 1.5
    if a in ("amount", "monto_base", "salario_mensual_cop", "tasa_anual"): return 1000000
    if a == "priority": return "media"
    if a in ("variables", "signers", "column_mapping"): return []
    if a in ("value",): return "test_value"
    if a == "key": return "test_key"
    if a == "nombre": return "Test Nombre"
    if a == "email": return "test@lexai.co"
    if a == "materia": return "laboral"
    if a in ("cedula", "expediente"): return "1234567890"
    if a == "to_phone": return "+573001112233"
    if a == "fuente": return "rama_judicial"
    if "fecha" in a and a.endswith("_start"): return "2026-01-01"
    if "fecha" in a and a.endswith("_end"): return "2026-01-31"
    if "fecha" in a: return "2026-01-15"
    if "period" in a and a.endswith("_start"): return "2026-01-01"
    if "period" in a and a.endswith("_end"): return "2026-01-31"
    if a == "file": return None
    return "test_value"


def call_endpoint(
    api: str, token: str, tool_name: str, args: dict[str, Any],
    matter_id: Optional[str], document_id: Optional[str],
) -> dict[str, Any]:
    """POST /v1/admin/tools/test/{tool_name}."""
    extra_ctx: dict[str, Any] = {}
    if matter_id: extra_ctx["matter_id"] = matter_id
    if document_id: extra_ctx["document_id"] = document_id

    body = json.dumps({"args": args, "extra_ctx": extra_ctx or None}).encode("utf-8")
    url = f"{api.rstrip('/')}/v1/admin/tools/test/{tool_name}"
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "lexai-tools-test/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return {"http_status": r.status, "body": json.loads(r.read().decode("utf-8"))}
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8")
        except Exception:
            pass
        return {"http_status": e.code, "body": {"error": body_text[:500]}}
    except Exception as e:
        return {"http_status": 0, "body": {"error": f"{type(e).__name__}: {e}"[:500]}}


def classify(spec: dict, http_status: int, body: dict) -> tuple[str, Optional[str]]:
    """Classify the outcome · returns (status_label, reason)."""
    if http_status == 0:
        return "NETWORK_ERROR", body.get("error", "")
    if http_status == 401:
        return "AUTH_FAIL", "401 unauthorized · token expired/invalid"
    if http_status == 403:
        return "ADMIN_REQUIRED", "403 forbidden · need admin role or set LEXAI_TOOL_TEST_ALLOW_ALL=true"
    if http_status == 404:
        return "ENDPOINT_MISSING", "404 · endpoint not deployed yet · Railway redeploy needed"
    if http_status >= 500:
        return "SERVER_ERROR", str(body.get("error", body))[:300]
    if not body.get("ok"):
        soft = body.get("soft_error") or body.get("error") or "unknown"
        err_type = body.get("error_type") or "soft"
        return "FAIL", f"[{err_type}] {soft}"
    return "PASS", None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--api", required=True, help="Backend base URL")
    p.add_argument("--token", required=True, help="Supabase JWT token")
    p.add_argument("--matter-id", help="Real matter UUID for context")
    p.add_argument("--document-id", help="Real matter_document UUID")
    p.add_argument("--only", help="Test only ONE tool by name (debug)")
    p.add_argument("--skip-safety", default="UI_BRIDGE",
                   help="Comma-separated safety_class to skip (default: UI_BRIDGE)")
    p.add_argument("--stop-on-fail", action="store_true",
                   help="Stop at first FAIL · for fix-test-fix loop")
    args = p.parse_args()

    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    skip_safety = set(s.strip() for s in args.skip_safety.split(",") if s.strip())

    if args.only:
        catalog = [c for c in catalog if c["name"] == args.only]
        if not catalog:
            print(f"FATAL: tool '{args.only}' not in catalog")
            return 2

    print(f"Testing {len(catalog)} tool(s) against {args.api}")
    print(f"Skipping safety classes: {sorted(skip_safety)}")
    results: list[dict] = []

    for spec in catalog:
        sc = spec["safety_class"]
        if sc in skip_safety:
            results.append({
                **spec, "status": "SKIP", "reason": f"safety_class {sc} skipped",
                "duration_ms": 0, "http_status": None,
            })
            print(f"  [SKIP   ] {spec['name']:36s} ({sc})")
            continue

        mock_args = build_mock_args(
            spec["required_args"],
            matter_id=args.matter_id, document_id=args.document_id,
            tool_name=spec["name"],
        )
        started = time.time()
        resp = call_endpoint(
            args.api, args.token, spec["name"], mock_args,
            args.matter_id, args.document_id,
        )
        duration_ms = int((time.time() - started) * 1000)
        status, reason = classify(spec, resp["http_status"], resp["body"])

        results.append({
            **spec,
            "status": status,
            "reason": reason or "",
            "http_status": resp["http_status"],
            "duration_ms": duration_ms,
            "body_sample": json.dumps(resp["body"], ensure_ascii=False, default=str)[:200],
        })
        line = f"  [{status:8s}] {spec['name']:36s} ({duration_ms:5d}ms) {reason or ''}"
        print(line[:140])

        if args.stop_on_fail and status not in ("PASS", "SKIP"):
            print(f"\nSTOPPED at {spec['name']} · status={status}")
            print(f"Reason: {reason}")
            print(f"Full body: {json.dumps(resp['body'], ensure_ascii=False, default=str)[:1000]}")
            break

    # Reports
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = RESULTS_DIR / f"tools_remote_{ts}.csv"
    json_path = RESULTS_DIR / f"tools_remote_{ts}.json"

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "name", "safety_class", "module", "status", "http_status",
            "duration_ms", "reason", "body_sample",
        ])
        for r in results:
            w.writerow([
                r["name"], r["safety_class"], r.get("module", ""),
                r["status"], r["http_status"], r["duration_ms"],
                r["reason"], r.get("body_sample", ""),
            ])
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    # Summary
    print("\n" + "=" * 70)
    print(f"SUMMARY · {len(results)} tools")
    print("=" * 70)
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    for status in sorted(by_status, key=lambda s: -by_status[s]):
        print(f"  {status:14s} {by_status[status]}")
    print(f"\nReports:")
    print(f"  CSV  · {csv_path}")
    print(f"  JSON · {json_path}")

    failed = sum(1 for r in results if r["status"] not in ("PASS", "SKIP"))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
