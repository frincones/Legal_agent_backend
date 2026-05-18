"""Test runner for all 117 voice tools (also reachable via chat skills).

Usage:
  # Dry-run · validates each tool is registered + has descriptor schema · NO
  # actual execution · NO DB / OpenAI / external calls · instant feedback.
  python -m scripts.test_all_tools --dry-run

  # Live · executes each tool with minimal mock args against a real backend.
  # Requires DATABASE_URL + OPENAI_API_KEY · uses provided fixtures.
  python -m scripts.test_all_tools --live \
      --firm-id <UUID> --user-id <UUID> \
      [--matter-id <UUID>] [--document-id <UUID>]

  # Live · skip tools that need a real matter / document fixture you didn't pass.
  python -m scripts.test_all_tools --live --firm-id ... --user-id ... --skip-needs-fixtures

Output:
  - Console: per-tool PASS / FAIL / SKIP with reason.
  - File: test-results/tools_report_YYYYMMDD_HHMMSS.csv
  - File: test-results/tools_report_YYYYMMDD_HHMMSS.json (full payloads)

The runner imports api.voice (triggers register_tool() side effects),
then iterates the global _tool_registry and matches against tools_catalog.json
to know what mock args each tool needs.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import inspect
import json
import logging
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("test_all_tools")
HERE = Path(__file__).resolve().parent
CATALOG_PATH = HERE / "tools_catalog.json"
RESULTS_DIR = HERE.parent / "test-results"

# ──────────────────────────────────────────────────────────────
# Mock arg builders · one per common required_arg name
# ──────────────────────────────────────────────────────────────


def build_mock_args(
    required: list[str],
    *,
    matter_id: Optional[str] = None,
    document_id: Optional[str] = None,
) -> dict[str, Any]:
    """Best-effort mock for each known arg name."""
    out: dict[str, Any] = {}
    for arg in required:
        out[arg] = _mock_for(arg, matter_id=matter_id, document_id=document_id)
    return out


def _mock_for(arg: str, matter_id: Optional[str], document_id: Optional[str]) -> Any:
    # Catalog of known arg name → mock value
    arg_low = arg.lower()
    if arg_low in ("matter_id", "matterid"):
        return matter_id or "00000000-0000-0000-0000-000000000000"
    if arg_low in (
        "matter_document_id", "matterdocumentid", "document_id", "documentid",
        "document_a_id", "document_b_id",
    ):
        return document_id or "00000000-0000-0000-0000-000000000000"
    if arg_low in ("deadline_id", "task_id", "comment_id", "redline_id",
                   "redline_set_id", "envelope_id", "trust_account_id",
                   "judge_id", "subject_id_value", "rule_id"):
        return "00000000-0000-0000-0000-000000000000"
    if arg_low in ("query", "q", "title", "kind", "body", "description", "find",
                   "replace", "section", "heading", "message", "modal_id",
                   "form_id", "field", "command", "subagent", "task",
                   "session_token", "slug", "citation_ref", "norm_reference",
                   "tipo_accion", "causa", "tipo", "subject_kind",
                   "metric", "target_type", "path"):
        return f"test_{arg}"
    if arg_low in ("days", "months"):
        return 30
    if arg_low in ("hours",):
        return 1.5
    if arg_low in ("amount", "monto_base", "salario_mensual_cop", "tasa_anual"):
        return 1000000
    if arg_low in ("priority",):
        return "media"
    if arg_low in ("variables", "signers", "column_mapping"):
        return []
    if arg_low in ("value",):
        return "test_value"
    if arg_low in ("key",):
        return "test_key"
    if arg_low in ("nombre",):
        return "Test Nombre"
    if arg_low in ("email",):
        return "test@lexai.co"
    if arg_low in ("materia",):
        return "laboral"
    if arg_low in ("cedula", "expediente"):
        return "1234567890"
    if arg_low in ("to_phone",):
        return "+573001112233"
    if arg_low in ("template_body", "subject", "context", "prompt", "facts",
                   "text", "document_text", "document"):
        return "Texto de prueba"
    if arg_low in ("file",):
        return None  # will likely fail validation, which is fine
    if "fecha" in arg_low:
        return "2026-01-15"
    if "period" in arg_low and arg_low.endswith("_start"):
        return "2026-01-01"
    if "period" in arg_low and arg_low.endswith("_end"):
        return "2026-01-31"
    if arg_low == "firm_id":
        return "00000000-0000-0000-0000-000000000000"
    if arg_low == "fuente":
        return "rama_judicial"
    # Default: empty string, will likely trigger validation error · we capture it.
    return ""


# ──────────────────────────────────────────────────────────────
# Result model
# ──────────────────────────────────────────────────────────────


@dataclass
class ToolTestResult:
    name: str
    module: str
    safety_class: str
    registered: bool = False
    descriptor_ok: bool = False
    executed: bool = False
    status: str = "PENDING"           # PASS · FAIL · SKIP · NOT_REGISTERED
    duration_ms: int = 0
    error: Optional[str] = None
    error_type: Optional[str] = None
    notes: list[str] = field(default_factory=list)
    sample_result_keys: list[str] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# Runner
# ──────────────────────────────────────────────────────────────


_REGISTER_RE = None


def _parse_main_register_calls() -> dict[str, str]:
    """Parse main.py · return {tool_name: callable_name} for every register_tool().

    Avoids running lifespan() (which requires DB + OpenAI). Pure text scan
    of main.py for the pattern: register_tool("X", Y_tool)
    """
    import re
    global _REGISTER_RE
    if _REGISTER_RE is None:
        _REGISTER_RE = re.compile(r"""register_tool\(\s*["']([\w_]+)["']\s*,\s*([\w_]+)\s*\)""")
    main_path = Path(__file__).resolve().parent.parent / "main.py"
    text = main_path.read_text(encoding="utf-8")
    pairs: dict[str, str] = {}
    for m in _REGISTER_RE.finditer(text):
        pairs[m.group(1)] = m.group(2)
    return pairs


def _try_import_callable(callable_name: str) -> tuple[bool, Optional[str]]:
    """Search main.py imports for callable_name, try importing it.

    Returns (ok, error_message). We accept multiple modules · scan
    `from X import Y` lines for callable_name and try each.
    """
    import re
    main_path = Path(__file__).resolve().parent.parent / "main.py"
    text = main_path.read_text(encoding="utf-8")
    import_re = re.compile(
        rf"from\s+([\w\.]+)\s+import[^()]*\b{re.escape(callable_name)}\b",
        re.MULTILINE,
    )
    multiline_re = re.compile(
        rf"from\s+([\w\.]+)\s+import\s*\([^)]*\b{re.escape(callable_name)}\b[^)]*\)",
        re.MULTILINE | re.DOTALL,
    )
    candidates: list[str] = []
    for rx in (import_re, multiline_re):
        for m in rx.finditer(text):
            mod = m.group(1)
            if mod not in candidates:
                candidates.append(mod)
    if not candidates:
        return False, f"no import statement for {callable_name} in main.py"
    last_err = None
    for mod_path in candidates:
        try:
            mod = __import__(mod_path, fromlist=[callable_name])
            fn = getattr(mod, callable_name, None)
            if fn is None:
                last_err = f"{mod_path} has no attr {callable_name}"
                continue
            if not callable(fn):
                last_err = f"{mod_path}.{callable_name} is not callable"
                continue
            # Check signature roughly matches (args, ctx).
            try:
                sig = inspect.signature(fn)
                params = list(sig.parameters.keys())
                # Accept (args, ctx) or (args=..., ctx=...)
                if not any(p in params for p in ("args",)):
                    last_err = f"{mod_path}.{callable_name} signature missing 'args' param"
                    continue
            except (ValueError, TypeError):
                pass  # builtins / C funcs
            return True, None
        except Exception as e:
            last_err = f"{mod_path}: {type(e).__name__}: {str(e)[:140]}"
            continue
    return False, last_err or f"all imports failed for {callable_name}"


async def run_dry(catalog: list[dict]) -> list[ToolTestResult]:
    """Validate cataloged tools by static parsing + module import-check.

    For each catalog entry:
      1. Check main.py has a register_tool("name", ...) call (any pairing).
      2. Try importing the function from spec["module"] directly. The convention
         is callable_name = "<tool_name>_tool" inside spec["module"].
      3. Verify signature has 'args' parameter.

    Does NOT execute the lifespan (which requires DB + OpenAI).
    """
    print("Parsing main.py for register_tool() calls…")
    pairs = _parse_main_register_calls()
    print(f"  found {len(pairs)} register_tool() calls in main.py")

    cat_names = {s["name"] for s in catalog}
    results: list[ToolTestResult] = []

    for spec in catalog:
        r = ToolTestResult(
            name=spec["name"],
            module=spec["module"],
            safety_class=spec["safety_class"],
        )
        if spec["name"] not in pairs:
            r.status = "NOT_REGISTERED"
            r.notes.append("No register_tool() call in main.py")
            results.append(r)
            continue
        r.registered = True

        callable_name = pairs[spec["name"]]
        # spec["module"] points to the SOURCE module · resolve it from there.
        # The callable name from register_tool may differ (e.g. spec.module
        # ends in '.calc' but register_tool callable is calc_liquidacion_tool).
        source_module = spec["module"]
        # If spec.module ends with .x but no dot, append nothing. Trust catalog.
        ok, err = _import_from_module(source_module, callable_name)
        if ok:
            r.descriptor_ok = True
            r.status = "PASS"
            r.notes.append(f"register + import OK from {source_module}.{callable_name}")
        else:
            r.status = "FAIL"
            r.error = err
            r.error_type = "ImportError"

        results.append(r)

    # Orphans: registered in main.py but not in our catalog · update catalog.
    for tool_name, callable_name in pairs.items():
        if tool_name not in cat_names:
            results.append(ToolTestResult(
                name=tool_name,
                module=callable_name,
                safety_class="?",
                registered=True,
                status="ORPHAN",
                notes=[f"In main.py but NOT in catalog · {callable_name}"],
            ))

    return results


def _import_from_module(module_path: str, callable_name: str) -> tuple[bool, Optional[str]]:
    """Import callable from a specific module path · returns (ok, error)."""
    try:
        mod = __import__(module_path, fromlist=[callable_name])
    except Exception as e:
        return False, f"import {module_path}: {type(e).__name__}: {str(e)[:160]}"
    fn = getattr(mod, callable_name, None)
    if fn is None:
        return False, f"{module_path} has no attr {callable_name}"
    if not callable(fn):
        return False, f"{module_path}.{callable_name} is not callable"
    try:
        sig = inspect.signature(fn)
        params = list(sig.parameters.keys())
        if "args" not in params:
            return False, f"{module_path}.{callable_name} signature missing 'args' param ({params})"
    except (ValueError, TypeError):
        pass
    return True, None


async def run_live(
    catalog: list[dict],
    *,
    firm_id: str,
    user_id: str,
    matter_id: Optional[str],
    document_id: Optional[str],
    skip_needs_fixtures: bool,
    skip_safety: set[str],
) -> list[ToolTestResult]:
    """Execute each tool with mock args · captures result/error."""
    await _trigger_lifespan()

    from api.voice import _tool_registry
    registry = _tool_registry

    ctx_base = {
        "firm_id": firm_id,
        "user_id": user_id,
        "matter_id": matter_id,
        "document_id": document_id,
        "subagent_chain": ["test_runner"],
    }

    results: list[ToolTestResult] = []
    for spec in catalog:
        r = ToolTestResult(
            name=spec["name"],
            module=spec["module"],
            safety_class=spec["safety_class"],
        )

        if spec["name"] not in registry:
            r.status = "NOT_REGISTERED"
            results.append(r)
            continue
        r.registered = True

        if spec["safety_class"] in skip_safety:
            r.status = "SKIP"
            r.notes.append(f"Skipped (safety_class in skip set)")
            results.append(r)
            continue

        if skip_needs_fixtures and (
            (spec["needs_matter"] and not matter_id) or
            (spec["needs_doc"] and not document_id)
        ):
            r.status = "SKIP"
            r.notes.append("Needs real fixture not provided")
            results.append(r)
            continue

        fn = registry[spec["name"]]
        mock_args = build_mock_args(
            spec["required_args"],
            matter_id=matter_id, document_id=document_id,
        )
        started = time.time()
        try:
            # All tools follow signature: async def x(args: dict, ctx: dict)
            result = await fn(args=mock_args, ctx=dict(ctx_base))
            r.executed = True
            r.duration_ms = int((time.time() - started) * 1000)
            if isinstance(result, dict):
                r.sample_result_keys = list(result.keys())[:8]
                # Many tools return {"error": "..."} or {"ok": false} as a soft fail.
                if "error" in result:
                    r.status = "FAIL"
                    r.error = str(result["error"])[:240]
                    r.error_type = "tool_returned_error"
                elif result.get("ok") is False:
                    r.status = "FAIL"
                    r.error = str(result.get("reason") or result.get("error") or "ok=false")[:240]
                    r.error_type = "tool_returned_ok_false"
                else:
                    r.status = "PASS"
            else:
                r.status = "PASS"
                r.sample_result_keys = ["<non-dict>"]
        except Exception as e:
            r.duration_ms = int((time.time() - started) * 1000)
            r.status = "FAIL"
            r.error = str(e)[:240]
            r.error_type = type(e).__name__

        results.append(r)
        print(f"  [{r.status:6s}] {r.name:36s} ({r.duration_ms:5d}ms) {r.error or ''}"[:120])

    return results


# ──────────────────────────────────────────────────────────────
# Report writers
# ──────────────────────────────────────────────────────────────


def write_reports(results: list[ToolTestResult], mode: str) -> tuple[Path, Path]:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = RESULTS_DIR / f"tools_report_{mode}_{ts}.csv"
    json_path = RESULTS_DIR / f"tools_report_{mode}_{ts}.json"

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "name", "module", "safety_class", "registered", "descriptor_ok",
            "executed", "status", "duration_ms", "error_type", "error",
            "sample_result_keys", "notes",
        ])
        for r in results:
            w.writerow([
                r.name, r.module, r.safety_class, r.registered, r.descriptor_ok,
                r.executed, r.status, r.duration_ms, r.error_type or "",
                r.error or "", "|".join(r.sample_result_keys), " · ".join(r.notes),
            ])

    with json_path.open("w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in results], f, ensure_ascii=False, indent=2)

    return csv_path, json_path


def print_summary(results: list[ToolTestResult]) -> None:
    by_status: dict[str, int] = {}
    by_safety_status: dict[tuple[str, str], int] = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        key = (r.safety_class, r.status)
        by_safety_status[key] = by_safety_status.get(key, 0) + 1

    print("\n" + "=" * 70)
    print(f"SUMMARY · {sum(by_status.values())} tools")
    print("=" * 70)
    for status, count in sorted(by_status.items(), key=lambda x: -x[1]):
        print(f"  {status:18s} {count}")
    print("\nBreakdown · safety × status:")
    safety_classes = sorted({s for s, _ in by_safety_status})
    statuses = sorted({s for _, s in by_safety_status})
    print(f"  {'safety_class':24s} " + " ".join(f"{s:>10s}" for s in statuses))
    for sc in safety_classes:
        row = [f"{by_safety_status.get((sc, s), 0):>10d}" for s in statuses]
        print(f"  {sc:24s} " + " ".join(row))

    failed = [r for r in results if r.status == "FAIL"]
    if failed:
        print(f"\nFAILED tools ({len(failed)}):")
        for r in failed[:30]:
            print(f"  - {r.name:35s} [{r.safety_class}] {r.error_type or '?'}: {r.error or ''}"[:120])
        if len(failed) > 30:
            print(f"  ... and {len(failed) - 30} more (see CSV)")


# ──────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Test all 117 voice tools.")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="Only check registration + descriptors · no execution")
    mode.add_argument("--live", action="store_true",
                      help="Execute each tool with mock args (requires DB + creds)")
    p.add_argument("--firm-id", help="Real firm_id for ctx (live mode)")
    p.add_argument("--user-id", help="Real user_id for ctx (live mode)")
    p.add_argument("--matter-id", help="Real matter_id fixture (optional)")
    p.add_argument("--document-id", help="Real matter_document_id fixture (optional)")
    p.add_argument("--skip-needs-fixtures", action="store_true",
                   help="Skip tools that require matter/doc fixtures you didn't pass")
    p.add_argument("--skip-safety", default="EXTERNAL_SEND,UI_BRIDGE",
                   help="Comma-separated safety_class values to skip (default: EXTERNAL_SEND,UI_BRIDGE)")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args()


def main_cli() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )

    if not CATALOG_PATH.exists():
        print(f"FATAL: catalog not found at {CATALOG_PATH}")
        return 2
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    print(f"Loaded catalog · {len(catalog)} tools")

    if args.dry_run:
        results = asyncio.run(run_dry(catalog))
        mode_label = "dry"
    else:
        if not args.firm_id or not args.user_id:
            print("FATAL: --live requires --firm-id and --user-id")
            return 2
        skip_safety = set(s.strip() for s in args.skip_safety.split(",") if s.strip())
        results = asyncio.run(run_live(
            catalog,
            firm_id=args.firm_id,
            user_id=args.user_id,
            matter_id=args.matter_id,
            document_id=args.document_id,
            skip_needs_fixtures=args.skip_needs_fixtures,
            skip_safety=skip_safety,
        ))
        mode_label = "live"

    csv_p, json_p = write_reports(results, mode_label)
    print_summary(results)
    print(f"\nReports:\n  CSV  · {csv_p}\n  JSON · {json_p}")

    failed = sum(1 for r in results if r.status == "FAIL")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main_cli())
