"""Smoke test directo de legal_sources/igac_source.py contra Supabase real."""
import asyncio
import os
import sys
import asyncpg

# Auto-load .env
_env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
if os.path.exists(_env_path):
    for line in open(_env_path, "r", encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

import urllib.parse as _up
DB_URL_raw = os.getenv("DATABASE_URL")
# Algunos passwords vienen con URL-encoded percent sequences. asyncpg en 0.30+
# requiere el password ya decodificado en el dsn.
def _decode_pwd_in_dsn(dsn: str) -> str:
    if not dsn or "@" not in dsn:
        return dsn
    scheme, _, rest = dsn.partition("://")
    creds, _, host = rest.rpartition("@")
    user, _, pwd = creds.partition(":")
    if not pwd:
        return dsn
    return f"{scheme}://{user}:{_up.unquote(pwd)}@{host}"

DB_URL = _decode_pwd_in_dsn(DB_URL_raw) if DB_URL_raw else None
if not DB_URL:
    # Build from supabase params if not in env
    # postgresql://postgres.osyrwsbruydcyhdjvjpv:[PASSWORD]@aws-0-us-east-1.pooler.supabase.com:6543/postgres
    pwd = os.getenv("SUPABASE_DB_PASSWORD")
    if not pwd:
        print("Set DATABASE_URL or SUPABASE_DB_PASSWORD")
        sys.exit(1)
    DB_URL = f"postgresql://postgres.osyrwsbruydcyhdjvjpv:{pwd}@aws-0-us-east-1.pooler.supabase.com:6543/postgres"

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from legal_sources.igac_source import (
    parse_cedula_catastral,
    verify_cedula_estructura,
    verify_cedula_full,
)


async def main():
    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=2)
    try:
        # Test 1: parse cedula
        cases = [
            ("110011234567890", "11001"),
            ("05001-12345", "05001"),
            ("11001 0000 0000 00", "11001"),
            ("abc", None),
            ("123", None),
            ("76001000000000", "76001"),
        ]
        print("\n=== parse_cedula_catastral ===")
        for raw, expected_divipola in cases:
            p = parse_cedula_catastral(raw)
            actual = p["divipola"] if p else None
            ok = actual == expected_divipola
            print(f"  [{'PASS' if ok else 'FAIL'}] {raw!r:30} -> divipola={actual} (esperado {expected_divipola})")

        # Test 2: verify estructura
        print("\n=== verify_cedula_estructura ===")
        for raw, expected_estado in [
            ("11001000000000", "valida"),
            ("05001000000000", "valida"),
            ("76001000000000", "valida"),
            ("99999000000000", "divipola_invalido"),
            ("abc", "unparseable"),
        ]:
            r = await verify_cedula_estructura(pool, raw)
            ok = r["estado"] == expected_estado
            mark = "PASS" if ok else "FAIL"
            extra = f" municipio={r.get('municipio','')}" if r.get("municipio") else ""
            print(f"  [{mark}] {raw:20} -> estado={r['estado']}{extra}")

        # Test 3: verify_cedula_full (Bogota intentara enriquecer)
        print("\n=== verify_cedula_full (Bogota IDECA) ===")
        # Use a known Bogota cedula format. Even if IDECA falla, estructura_valida es OK
        r = await verify_cedula_full(pool, "110010000000000")
        print(f"  Bogota cedula -> estado={r['estado']}, fuente={r.get('fuente')}, gestor={r.get('gestor_cat')}")
        ok = r["estado"] in ("estructura_valida", "verificada_cache", "verificada_live", "valida")
        print(f"  [{'PASS' if ok else 'FAIL'}] full chain devuelve estado valido")

    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
