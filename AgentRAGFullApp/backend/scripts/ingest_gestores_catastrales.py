"""Sprint L8 - Ingest gestores catastrales (DIVIPOLA -> IGAC/local).

Fuente: https://www.datos.gov.co/resource/bhcx-bx97.json (~1,103 municipios)

Idempotente: ON CONFLICT (divipola) DO UPDATE.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.parse

SUPABASE_REF = os.getenv("SUPABASE_REF", "osyrwsbruydcyhdjvjpv")
SUPABASE_TOKEN = os.getenv("SUPABASE_ACCESS_TOKEN")
if not SUPABASE_TOKEN:
    raise SystemExit("SUPABASE_ACCESS_TOKEN env var required")
SUPABASE_API = f"https://api.supabase.com/v1/projects/{SUPABASE_REF}/database/query"

DATASET_URL = "https://www.datos.gov.co/resource/bhcx-bx97.json"


def fetch_page(offset: int, limit: int) -> list[dict]:
    params = {
        "$select": "divipola,municipio,departamen,gestor_cat,gestor_con,estado_act,mparea",
        "$limit": str(limit),
        "$offset": str(offset),
        "$order": "divipola",
    }
    url = f"{DATASET_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def supabase_query(sql: str):
    body = json.dumps({"query": sql}).encode("utf-8")
    req = urllib.request.Request(
        SUPABASE_API,
        method="POST",
        headers={
            "Authorization": f"Bearer {SUPABASE_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
        data=body,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {e.read().decode()[:300]}")
        return None


def q(v):
    if v is None or v == "":
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("'", "''")
    return f"'{s}'"


def main():
    print("[L8] Ingest gestores catastrales -> gestores_catastrales")
    started = time.time()
    offset = 0
    batch = 500
    total_inserted = 0

    while True:
        rows = fetch_page(offset, batch)
        if not rows:
            break

        # Dedup por divipola en el batch
        seen = {}
        for r in rows:
            d = (r.get("divipola") or "").strip()
            if not d:
                continue
            seen[d] = r
        rows_dedup = list(seen.values())

        if rows_dedup:
            values_parts = []
            for r in rows_dedup:
                try:
                    area = float(r.get("mparea", 0))
                except (ValueError, TypeError):
                    area = None
                values_parts.append(
                    f"({q(r.get('divipola'))}, {q(r.get('municipio'))}, "
                    f"{q(r.get('departamen'))}, {q(r.get('gestor_cat'))}, "
                    f"{q(r.get('gestor_con'))}, {q(r.get('estado_act'))}, "
                    f"{q(area)})"
                )
            sql = f"""
            insert into gestores_catastrales
              (divipola, municipio, departamento, gestor_cat, gestor_con, estado_act, area_km2)
            values
              {', '.join(values_parts)}
            on conflict (divipola) do update set
              municipio = excluded.municipio,
              departamento = excluded.departamento,
              gestor_cat = excluded.gestor_cat,
              gestor_con = excluded.gestor_con,
              estado_act = excluded.estado_act,
              area_km2 = excluded.area_km2,
              updated_at = now()
            ;
            """
            res = supabase_query(sql)
            if res is not None:
                total_inserted += len(rows_dedup)

        print(f"  offset {offset:5d} | fetched {len(rows):4d} | dedup {len(rows_dedup):4d} | total inserted {total_inserted:5d}")

        if len(rows) < batch:
            break
        offset += batch
        time.sleep(0.2)

    elapsed = time.time() - started
    print(f"\n[L8] DONE in {elapsed:.1f}s")
    r = supabase_query("select count(*) as total, count(distinct gestor_cat) as gestores from gestores_catastrales;")
    print(f"  gestores_catastrales: {r}")
    r = supabase_query("select gestor_cat, count(*) from gestores_catastrales group by gestor_cat order by count(*) desc limit 15;")
    if r:
        for row in r:
            print(f"  {row.get('gestor_cat','?'):30} {row.get('count',0):5d}")


if __name__ == "__main__":
    main()
