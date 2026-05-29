"""Sprint M19.30 · Seed UN solo template (poder-especial) en firm_skills.

No duplica el `/redactar/poder` del sprint H porque ese tiene frontmatter
genérico; este expone `frontmatter->>'doc_type' = 'poder_especial'` para
que el renderer Claude (camino B) lo resuelva correctamente al recibir
?engine=claude para document_id con doc_type=poder_especial.

Genera: 2026_05_28_seed_m1930_poder_especial.sql
"""
from __future__ import annotations

import os, sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Reusar parser+builder del script previo
from scripts.seed_12_new_templates_co import (
    TEMPLATES_DIR, parse_skill_md, build_insert,
)


TEMPLATE = {
    "file": "01_notarial_poder_especial_co.md",
    "command": "/redactar/poder-especial",
    "category": "drafting",
}


def main() -> int:
    path = TEMPLATES_DIR / TEMPLATE["file"]
    if not path.exists():
        print(f"ERROR: missing {path}", file=sys.stderr)
        return 1
    fm, body = parse_skill_md(path.read_text(encoding="utf-8"))
    insert = build_insert(TEMPLATE, fm, body)
    sql = f"""-- ============================================================================
-- Sprint M19.30 · Seed 1 template (poder-especial) para Claude renderer
-- ============================================================================
-- Único builtin con frontmatter->>'doc_type' = 'poder_especial' que el
-- renderer Claude resuelve para ?engine=claude. Idempotente.
-- ============================================================================

begin;

{insert}

do $$
declare v_id uuid;
begin
  select id into v_id from firm_skills
   where firm_id is null
     and command = '/redactar/poder-especial'
     and status = 'published';
  if v_id is null then
    raise exception 'M19.30 seed poder-especial NOT FOUND after insert';
  end if;
  raise notice 'M19.30 poder-especial seeded id=%', v_id;
end$$;

commit;
"""
    out = Path(__file__).resolve().parent.parent / "storage" / "schemas" / \
          "2026_05_28_seed_m1930_poder_especial.sql"
    out.write_text(sql, encoding="utf-8")
    print(f"OK · wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
