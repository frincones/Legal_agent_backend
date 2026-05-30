"""Aplica la migración M20.01 (tool_call_audit) a Supabase prod.

Usa Supabase Management API (más confiable que asyncpg para migraciones idempotentes).

USO:
    cd backend
    python scripts/apply_m20_migration.py

Lee SUPABASE_ACCESS_TOKEN + SUPABASE_PROJECT_REF (del .env.local del frontend
o del .env del backend) y aplica el SQL idempotente. Es seguro re-correr.
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


SUPABASE_REF = (
    os.getenv("SUPABASE_PROJECT_REF")
    or os.getenv("SUPABASE_REF")
    or "osyrwsbruydcyhdjvjpv"
)
ACCESS_TOKEN = os.getenv("SUPABASE_ACCESS_TOKEN")
MGMT_API = f"https://api.supabase.com/v1/projects/{SUPABASE_REF}/database/query"


def supabase_query(sql: str) -> list[dict]:
    body = json.dumps({"query": sql}).encode("utf-8")
    req = urllib.request.Request(
        MGMT_API, method="POST",
        headers={
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "LegalAgentBot/1.0 (m20-migration)",
        },
        data=body,
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode("utf-8")
            result = json.loads(raw)
            return result if isinstance(result, list) else [result]
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {body_text[:500]}") from e


def main() -> int:
    if not ACCESS_TOKEN:
        print("ERROR: SUPABASE_ACCESS_TOKEN no encontrado.", file=sys.stderr)
        print(f"Buscado en: {_BACKEND_ROOT / '.env'} y {_FRONTEND_ROOT / '.env.local'}",
              file=sys.stderr)
        return 1

    sql_path = _BACKEND_ROOT / "storage" / "schemas" / "2026_05_29_sprint_m20_01_tool_call_audit.sql"
    if not sql_path.exists():
        print(f"ERROR: migración no encontrada en {sql_path}", file=sys.stderr)
        return 1

    sql = sql_path.read_text(encoding="utf-8")
    print(f"[1/3] Aplicando migración M20.01 (project {SUPABASE_REF})...")
    print(f"      archivo: {sql_path.name}")
    try:
        result = supabase_query(sql)
        print(f"      OK · respuesta: {json.dumps(result, default=str)[:200]}")
    except Exception as e:
        print(f"      ERROR aplicando SQL: {e}", file=sys.stderr)
        return 1

    print(f"[2/3] Verificando tool_call_audit existe...")
    try:
        check1 = supabase_query("""
            select count(*) as n from information_schema.tables
            where table_name = 'tool_call_audit'
        """)
        n_tables = check1[0]["n"] if check1 else 0
        print(f"      tool_call_audit existe: {n_tables > 0}")
    except Exception as e:
        print(f"      WARN verify falló: {e}", file=sys.stderr)
        n_tables = 0

    print(f"[3/3] Verificando columnas nuevas en generation_audit...")
    try:
        check2 = supabase_query("""
            select column_name from information_schema.columns
            where table_name = 'generation_audit'
              and column_name in ('cache_hit_tokens', 'orchestrator_kind')
        """)
        cols = [r["column_name"] for r in check2]
        print(f"      columnas nuevas: {cols}")
    except Exception as e:
        print(f"      WARN: {e}", file=sys.stderr)
        cols = []

    if n_tables and len(cols) == 2:
        print(f"\n[OK] Migracion M20.01 aplicada · S0.1 completo en Supabase prod")
        return 0
    print(f"\n[WARN] Migracion parcial · revisar manualmente", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
