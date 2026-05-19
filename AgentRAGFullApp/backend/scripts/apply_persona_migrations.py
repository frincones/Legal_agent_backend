"""Sprint P0 — Aplica las 3 migraciones de persona vía Supabase Management API.

Usa el SUPABASE_ACCESS_TOKEN (personal access token) disponible en el
.env.local del frontend o en el entorno del sistema.

Uso:
  python scripts/apply_persona_migrations.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# ── Rutas base ────────────────────────────────────────────────────────────────
_backend_root = Path(__file__).parent.parent
_frontend_root = Path(r"C:\Users\freddyrs\Desktop\Legal Demo\Legal_agent_Frontend")

# ── Cargar variables de entorno ───────────────────────────────────────────────
def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

# Backend .env primero, luego frontend .env.local (que tiene el PAT)
_load_env(_backend_root / ".env")
_load_env(_frontend_root / ".env.local")

SUPABASE_REF = os.getenv("SUPABASE_PROJECT_REF") or os.getenv("SUPABASE_REF", "osyrwsbruydcyhdjvjpv")
ACCESS_TOKEN = os.getenv("SUPABASE_ACCESS_TOKEN")
if not ACCESS_TOKEN:
    sys.exit(
        "ERROR: SUPABASE_ACCESS_TOKEN no encontrado.\n"
        "Asegúrate de que esté en .env.local o en el entorno del sistema.\n"
        f"Buscado en: {_frontend_root / '.env.local'}"
    )

MGMT_API = f"https://api.supabase.com/v1/projects/{SUPABASE_REF}/database/query"
SCHEMAS_DIR = _backend_root / "storage" / "schemas"

MIGRATIONS = [
    "2026_05_18_sprint_p0_persona_tables.sql",
    "2026_05_18_sprint_p0_persona_seed.sql",
    "2026_05_18_sprint_p1_skill_executions_persona.sql",
]


def supabase_query(sql: str) -> list[dict]:
    body = json.dumps({"query": sql}).encode("utf-8")
    req = urllib.request.Request(
        MGMT_API,
        method="POST",
        headers={
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "LegalAgentBot/1.0 (persona-migration)",
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


def apply_migration(filename: str) -> None:
    path = SCHEMAS_DIR / filename
    if not path.exists():
        sys.exit(f"ERROR: No se encontró {path}")
    sql = path.read_text(encoding="utf-8")
    print(f"\n{'='*70}")
    print(f"Aplicando: {filename}")
    print(f"{'='*70}")
    result = supabase_query(sql)
    snippet = json.dumps(result, ensure_ascii=False, default=str)[:300]
    print(f"Respuesta API: {snippet}")
    print("OK — migración aplicada.")


def verify_checks() -> bool:
    print(f"\n{'='*70}")
    print("VERIFICACIONES POST-MIGRACIÓN")
    print(f"{'='*70}")

    checks = [
        (
            "CHECK 1 — 7 tablas existen en information_schema",
            """
            SELECT count(*) as cnt
              FROM information_schema.tables
             WHERE table_schema = 'public'
               AND table_name IN (
                 'agent_personas','personality_modules','output_styles',
                 'firm_personality_overrides','user_personality_preferences',
                 'agent_personality_versions','session_personality_overrides'
               )
            """,
            lambda rows: int(rows[0].get("cnt", 0)) == 7,
            "esperado cnt=7",
        ),
        (
            "CHECK 2 — 1 fila en agent_personas (slug=lexai-co-senior-v1)",
            """
            SELECT count(*) as cnt
              FROM agent_personas
             WHERE slug = 'lexai-co-senior-v1'
            """,
            lambda rows: int(rows[0].get("cnt", 0)) == 1,
            "esperado cnt=1",
        ),
        (
            "CHECK 3 — 10 módulos en personality_modules para la persona system",
            """
            SELECT count(*) as cnt
              FROM personality_modules
             WHERE persona_id = (
               SELECT id FROM agent_personas
                WHERE slug = 'lexai-co-senior-v1' AND firm_id IS NULL
                LIMIT 1
             )
            """,
            lambda rows: int(rows[0].get("cnt", 0)) == 10,
            "esperado cnt=10",
        ),
        (
            "CHECK 4 — RLS habilitado en las 7 tablas",
            """
            SELECT relname, relrowsecurity
              FROM pg_class
             WHERE relname IN (
               'agent_personas','personality_modules','output_styles',
               'firm_personality_overrides','user_personality_preferences',
               'agent_personality_versions','session_personality_overrides'
             )
             ORDER BY relname
            """,
            lambda rows: all(r.get("relrowsecurity") is True for r in rows) and len(rows) == 7,
            "esperado 7 tablas con relrowsecurity=true",
        ),
        (
            "CHECK 5 — Columnas nuevas en skill_executions",
            """
            SELECT column_name, data_type
              FROM information_schema.columns
             WHERE table_name = 'skill_executions'
               AND column_name IN ('personality_version_id','personality_checksum')
             ORDER BY column_name
            """,
            lambda rows: len(rows) == 2,
            "esperado 2 columnas: personality_checksum + personality_version_id",
        ),
        (
            "CHECK 6 — RPC lexai_assemble_system_prompt(chat, /ask) retorna fila con prompt",
            """
            SELECT system_prompt IS NOT NULL   AS has_prompt,
                   length(system_prompt)       AS prompt_len,
                   checksum IS NOT NULL        AS has_checksum,
                   version_id IS NOT NULL      AS has_version
              FROM lexai_assemble_system_prompt(NULL::uuid, NULL::uuid, 'chat', '/ask', NULL)
            """,
            lambda rows: rows and rows[0].get("has_prompt") is True and (rows[0].get("prompt_len") or 0) > 100,
            "esperado has_prompt=true y prompt_len>100",
        ),
        (
            "CHECK 7 — RPC voice NO incluye S10 examples (gating de voz)",
            """
            SELECT system_prompt NOT LIKE '%EJEMPLO 1 — Consulta normativa%' AS examples_excluded,
                   length(system_prompt)                                       AS voice_len
              FROM lexai_assemble_system_prompt(NULL::uuid, NULL::uuid, 'voice', NULL, NULL)
            """,
            lambda rows: rows and rows[0].get("examples_excluded") is True,
            "esperado examples_excluded=true",
        ),
        (
            "CHECK 8 — Checksum determinista (2 llamadas = mismo checksum)",
            """
            SELECT
              (SELECT checksum FROM lexai_assemble_system_prompt(NULL::uuid, NULL::uuid, 'chat', '/ask', NULL))
              =
              (SELECT checksum FROM lexai_assemble_system_prompt(NULL::uuid, NULL::uuid, 'chat', '/ask', NULL))
              AS checksums_match
            """,
            lambda rows: rows and rows[0].get("checksums_match") is True,
            "esperado checksums_match=true",
        ),
    ]

    all_pass = True
    results_summary = []

    for label, sql, validator, hint in checks:
        try:
            rows = supabase_query(sql)
            passed = validator(rows)
            status = "PASS" if passed else "FAIL"
            snippet = json.dumps(rows, ensure_ascii=False, default=str)[:300]
            print(f"\n{label}")
            print(f"  Resultado: {snippet}")
            if not passed:
                print(f"  FALLO ({hint})")
            results_summary.append((label, status))
            if not passed:
                all_pass = False
        except Exception as exc:
            print(f"\n{label}")
            print(f"  ERROR: {exc}")
            results_summary.append((label, "ERROR"))
            all_pass = False
        time.sleep(0.4)

    print(f"\n{'='*70}")
    print("RESUMEN:")
    for label, status in results_summary:
        icon = "OK  " if status == "PASS" else "FAIL"
        print(f"  [{icon}] {label}")
    print(f"\nSTATUS GLOBAL: {'PASS' if all_pass else 'FAIL'}")
    print(f"{'='*70}")

    return all_pass


if __name__ == "__main__":
    print(f"SUPABASE_REF: {SUPABASE_REF}")
    print(f"MGMT_API:     {MGMT_API}")
    print(f"TOKEN:        {ACCESS_TOKEN[:12]}…")

    for mig in MIGRATIONS:
        apply_migration(mig)
        time.sleep(1.5)  # Leave time for Supabase to process the DDL

    time.sleep(2.0)
    ok = verify_checks()
    sys.exit(0 if ok else 1)
