"""Sprint M19.27 · Generador de la seed migration con 12 templates NUEVOS.

Lee los 12 SKILL.md de TEST Freddy/templates_review/ (los que NO duplican los
12 builtin del sprint_h) y emite SQL INSERT idempotente en firm_skills.

Output: backend/storage/schemas/2026_05_28_seed_m1927_12_new_builtin_skills.sql

Uso:
    python scripts/seed_12_new_templates_co.py

Re-ejecutable; sobreescribe el SQL.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

# Path al folder de templates. Layout actual:
#   ...\Legal Demo Back\Legal_agent_backend\AgentRAGFullApp\backend\scripts\<este_file>
#   ...\Legal Demo\Legal_agent_Frontend\TEST Freddy\templates_review\
# Subimos 5 niveles desde scripts/ hasta `Desktop\` y entramos por Legal Demo\...
import os as _os

_DEFAULT_TEMPLATES = (
    Path(__file__).resolve().parents[4]                         # backend/scripts -> .../Desktop
    / "Legal Demo"
    / "Legal_agent_Frontend"
    / "TEST Freddy"
    / "templates_review"
)
TEMPLATES_DIR = Path(_os.getenv("LEXAI_TEMPLATES_REVIEW_DIR") or _DEFAULT_TEMPLATES)

# Los 12 NUEVOS (que no duplican los builtin del sprint_h)
# El nombre es el de archivo (sin extensión), mapeado a su command final.
NEW_TEMPLATES: list[dict[str, str]] = [
    {"file": "03_notarial_revocatoria_poder_co.md",          "command": "/redactar/revocatoria-poder",         "category": "drafting"},
    {"file": "04_notarial_declaracion_extrajuicio_co.md",    "command": "/redactar/declaracion-extrajuicio",  "category": "drafting"},
    {"file": "06_judicial_demanda_civil_ordinaria_co.md",    "command": "/redactar/demanda-civil-ordinaria",  "category": "drafting"},
    {"file": "09_judicial_demanda_laboral_co.md",            "command": "/redactar/demanda-laboral",          "category": "drafting"},
    {"file": "10_judicial_demanda_admin_nulidad_co.md",      "command": "/redactar/demanda-admin-nulidad",    "category": "drafting"},
    {"file": "12_judicial_recurso_apelacion_co.md",          "command": "/redactar/recurso-apelacion",        "category": "drafting"},
    {"file": "13_judicial_contestacion_demanda_co.md",       "command": "/redactar/contestacion-demanda",     "category": "drafting"},
    {"file": "15_petitorio_requerimiento_extrajudicial_co.md", "command": "/redactar/requerimiento-extrajudicial", "category": "drafting"},
    {"file": "17_contractual_prestacion_servicios_co.md",    "command": "/redactar/prestacion-servicios",     "category": "drafting"},
    {"file": "18_contractual_compraventa_vehiculo_co.md",    "command": "/redactar/compraventa-vehiculo",     "category": "drafting"},
    {"file": "19_corporate_acta_asamblea_co.md",             "command": "/redactar/acta-asamblea",            "category": "drafting"},
    {"file": "21_conceptual_concepto_juridico_co.md",        "command": "/redactar/concepto-juridico",        "category": "analysis"},
]


_FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def parse_skill_md(md_text: str) -> tuple[dict[str, Any], str]:
    """Parser minimalista de SKILL.md (YAML frontmatter + body).

    NO usa pyyaml para evitar dep nueva. Parsea solo los campos que necesitamos.
    """
    m = _FRONT_RE.match(md_text.strip())
    if not m:
        return {}, md_text
    raw_yaml, body = m.group(1), m.group(2)
    fm: dict[str, Any] = {}
    # Parser muy simple: key: value | key: | y bloques con `|` o indented.
    lines = raw_yaml.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        val = rest.strip()
        if val == "|" or val == ">":
            # Bloque multilinea (indentado 2 espacios)
            i += 1
            parts: list[str] = []
            while i < len(lines) and (lines[i].startswith("  ") or not lines[i].strip()):
                parts.append(lines[i][2:] if lines[i].startswith("  ") else "")
                i += 1
            fm[key] = "\n".join(parts).rstrip()
            continue
        if val == "":
            # Posible lista
            i += 1
            items: list[str] = []
            while i < len(lines) and lines[i].lstrip().startswith("- "):
                items.append(lines[i].lstrip()[2:].strip())
                i += 1
            fm[key] = items
            continue
        # Valor simple
        fm[key] = val.strip().strip('"').strip("'")
        i += 1
    return fm, body.strip()


def sql_escape(s: str) -> str:
    """E'...' escape para PostgreSQL."""
    return s.replace("\\", "\\\\").replace("'", "''").replace("\n", "\\n").replace("\r", "")


def build_insert(template: dict[str, str], fm: dict[str, Any], body_md: str) -> str:
    """Construye INSERT idempotente con ON CONFLICT DO UPDATE para refresh."""
    command = template["command"]
    category = template["category"]
    name = fm.get("name") or command.rsplit("/", 1)[-1]
    description = fm.get("description") or f"Plantilla profesional para {command}"
    doc_type = fm.get("doc_type") or command.rsplit("/", 1)[-1].replace("-", "_")
    doc_family = fm.get("doc_family")
    default_scope = fm.get("default_scope") or "doc_type"
    jurisdiction = fm.get("jurisdiction") or "CO"
    version_str = fm.get("version") or "1.0.0"
    tier = fm.get("tier") or "public"

    # frontmatter jsonb — guardamos lo útil para selector LLM
    fm_persist = {
        "name": fm.get("name") or command,
        "description": description,
        "category": fm.get("category"),
        "doc_family": doc_family,
        "doc_type": doc_type,
        "default_scope": default_scope,
        "language": fm.get("language") or "es-CO",
        "jurisdiction": jurisdiction,
        "version": version_str,
        "tier": tier,
        "sources": fm.get("sources") or [],
    }
    fm_json = json.dumps(fm_persist, ensure_ascii=False, default=str)

    # El system_prompt es el body markdown completo (contiene When to use,
    # Document Structure, Style Conventions, etc.) — actúa como SKILL.md "system".
    # references_md queda null por ahora (puede llenarse después con Bibliography).
    return f"""
-- {command}
insert into firm_skills (
    firm_id, command, name, description, category, frontmatter,
    system_prompt, references_md, output_schema, jurisdiction,
    user_invocable, tier, status, version, metadata
) values (
    null, '{sql_escape(command)}',
    '{sql_escape(name)}',
    '{sql_escape(description)}',
    '{sql_escape(category)}',
    '{sql_escape(fm_json)}'::jsonb,
    E'{sql_escape(body_md)}',
    null,
    null,
    'CO',
    true, '{tier}', 'published', 1,
    '{{"seeded_by":"sprint_m1927","seed_date":"2026-05-28"}}'::jsonb
)
on conflict (firm_id, command, version) do update set
    name = excluded.name,
    description = excluded.description,
    category = excluded.category,
    frontmatter = excluded.frontmatter,
    system_prompt = excluded.system_prompt,
    tier = excluded.tier,
    status = 'published',
    updated_at = now();
"""


def main() -> int:
    if not TEMPLATES_DIR.exists():
        print(f"ERROR: templates dir missing: {TEMPLATES_DIR}", file=sys.stderr)
        return 1

    inserts: list[str] = []
    missing: list[str] = []
    for t in NEW_TEMPLATES:
        path = TEMPLATES_DIR / t["file"]
        if not path.exists():
            missing.append(str(path))
            continue
        md = path.read_text(encoding="utf-8")
        fm, body = parse_skill_md(md)
        inserts.append(build_insert(t, fm, body))

    if missing:
        print(f"WARNING: missing {len(missing)} template files:", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)

    out_sql = f"""-- ============================================================================
-- Sprint M19.27 · Seed 12 NEW builtin skills (no duplica sprint_h)
-- ============================================================================
-- Generado por scripts/seed_12_new_templates_co.py desde
-- TEST Freddy/templates_review/{{03,04,06,09,10,12,13,15,17,18,19,21}}_*.md
-- IDEMPOTENTE: ON CONFLICT (firm_id, command, version) DO UPDATE.
-- ============================================================================

begin;

{"".join(inserts)}

-- Verificación final
do $$
declare
  v_count int;
begin
  select count(*) into v_count
    from firm_skills
   where firm_id is null
     and status = 'published'
     and command in (
       '/redactar/revocatoria-poder',
       '/redactar/declaracion-extrajuicio',
       '/redactar/demanda-civil-ordinaria',
       '/redactar/demanda-laboral',
       '/redactar/demanda-admin-nulidad',
       '/redactar/recurso-apelacion',
       '/redactar/contestacion-demanda',
       '/redactar/requerimiento-extrajudicial',
       '/redactar/prestacion-servicios',
       '/redactar/compraventa-vehiculo',
       '/redactar/acta-asamblea',
       '/redactar/concepto-juridico'
     );
  if v_count < 12 then
    raise exception 'Sprint M19.27 seeded only % of 12 expected skills', v_count;
  end if;
  raise notice 'Sprint M19.27 seeded % skills OK', v_count;
end$$;

commit;
"""

    out_path = (
        Path(__file__).resolve().parent.parent
        / "storage"
        / "schemas"
        / "2026_05_28_seed_m1927_12_new_builtin_skills.sql"
    )
    out_path.write_text(out_sql, encoding="utf-8")
    print(f"OK · wrote {len(inserts)} INSERTs to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
