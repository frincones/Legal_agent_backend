"""Aplica las migraciones M20.11 (matter_context RPC + agent_memory FTS).

EJECUTAR TÚ MANUALMENTE:
    cd backend
    python scripts/apply_m20_11_migrations.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

_BACKEND_ROOT = Path(__file__).parent.parent
_FRONTEND_ROOT = Path(r"C:\Users\freddyrs\Desktop\Legal Demo\Legal_agent_Frontend")


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(_BACKEND_ROOT / ".env")
_load_env(_FRONTEND_ROOT / ".env.local")

SUPABASE_REF = os.getenv("SUPABASE_PROJECT_REF") or "osyrwsbruydcyhdjvjpv"
TOKEN = os.getenv("SUPABASE_ACCESS_TOKEN")
URL = f"https://api.supabase.com/v1/projects/{SUPABASE_REF}/database/query"

MIGRATIONS = [
    "2026_05_29_sprint_m20_11_matter_context_rpc.sql",
    "2026_05_29_sprint_m20_11_agent_memory_fts.sql",
]


def query(sql: str) -> dict:
    body = json.dumps({"query": sql}).encode("utf-8")
    req = urllib.request.Request(
        URL, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "LexAI/M20.11",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8')[:300]}") from e


def apply(name: str) -> bool:
    path = _BACKEND_ROOT / "storage" / "schemas" / name
    if not path.exists():
        print(f"ERROR: {path} no existe", file=sys.stderr)
        return False
    sql = path.read_text(encoding="utf-8")
    print(f"[apply] {name}...")
    try:
        result = query(sql)
        print(f"  OK · {json.dumps(result, default=str)[:120]}")
        return True
    except Exception as e:
        print(f"  ERR: {e}", file=sys.stderr)
        return False


def main() -> int:
    if not TOKEN:
        print("ERROR: SUPABASE_ACCESS_TOKEN no configurado", file=sys.stderr)
        return 1
    all_ok = True
    for m in MIGRATIONS:
        if not apply(m):
            all_ok = False

    print("\n=== Verificacion ===")
    try:
        rpcs = query(
            "select proname from pg_proc where proname in "
            "('lexai_matter_full_context', 'lexai_recall_memory')"
        )
        print(f"  RPCs creadas: {rpcs}")

        col = query(
            "select column_name from information_schema.columns "
            "where table_name = 'agent_memory' and column_name = 'value_tsv'"
        )
        print(f"  agent_memory.value_tsv: {col}")

        idx = query(
            "select indexname from pg_indexes where indexname = 'agent_memory_value_tsv_idx'"
        )
        print(f"  FTS index: {idx}")
    except Exception as e:
        print(f"  WARN verify failed: {e}", file=sys.stderr)

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
