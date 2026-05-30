"""Aplica la migración M20.01 (tool_call_audit) a Supabase prod.

EJECUTAR MANUALMENTE TÚ (no auto):

    cd backend
    python scripts/apply_m20_migration.py

Lee DATABASE_URL del .env y aplica el SQL idempotente. Es seguro re-correr.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).parent.parent


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def main() -> int:
    _load_env(_BACKEND_ROOT / ".env")
    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        print("ERROR: DATABASE_URL no encontrado en .env", file=sys.stderr)
        return 1

    sql_path = _BACKEND_ROOT / "storage" / "schemas" / "2026_05_29_sprint_m20_01_tool_call_audit.sql"
    if not sql_path.exists():
        print(f"ERROR: migración no encontrada en {sql_path}", file=sys.stderr)
        return 1
    sql = sql_path.read_text(encoding="utf-8")

    try:
        import asyncpg
    except ImportError:
        print("ERROR: asyncpg no instalado. pip install asyncpg", file=sys.stderr)
        return 1

    safe_dsn = dsn.split("@", 1)[1] if "@" in dsn else dsn
    print(f"[1/3] Conectando a Supabase ({safe_dsn[:60]}...)...")
    conn = await asyncpg.connect(dsn)
    try:
        print(f"[2/3] Ejecutando migración ({sql_path.name})...")
        await conn.execute(sql)
        print("      OK · migración aplicada")

        print("[3/3] Verificando estado post-migración...")
        n_tables = await conn.fetchval(
            "select count(*) from information_schema.tables where table_name = 'tool_call_audit'"
        )
        print(f"      tool_call_audit existe: {n_tables > 0}")

        n_cols = await conn.fetch(
            """select column_name from information_schema.columns
               where table_name = 'generation_audit'
                 and column_name in ('cache_hit_tokens', 'orchestrator_kind')"""
        )
        new_cols = [r["column_name"] for r in n_cols]
        print(f"      generation_audit columnas nuevas: {new_cols}")

        n_indices = await conn.fetchval(
            "select count(*) from pg_indexes where tablename = 'tool_call_audit'"
        )
        print(f"      índices tool_call_audit: {n_indices}")

        n_policies = await conn.fetchval(
            "select count(*) from pg_policies where tablename = 'tool_call_audit'"
        )
        print(f"      RLS policies tool_call_audit: {n_policies}")

        if n_tables and len(new_cols) == 2 and n_indices >= 4 and n_policies >= 2:
            print("\n✓ Migración OK · S0.1 completado")
            return 0
        print("\n⚠ Migración parcial · revisar manualmente", file=sys.stderr)
        return 1
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
