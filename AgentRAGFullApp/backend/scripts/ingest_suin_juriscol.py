"""Sprint L11 · Bulk ingest SUIN-Juriscol → leyes_normas

Descarga el dataset fiev-nid6 de datos.gov.co (Socrata SODA) paginado y lo
inserta en leyes_normas con su vigencia oficial.

Fuente: https://www.datos.gov.co/Justicia-y-Derecho/Lista-de-normas-cargadas-en-el-Sistema-nico-de-Inf/fiev-nid6

Total esperado: ~88,827 registros (al momento del último conteo)
Cobertura: LEY, DECRETO, RESOLUCION, ACUERDO, CIRCULAR, CODIGO, ACTO LEGISLATIVO,
           CONSTITUCION POLITICA, DIRECTIVA · 18 tipos · desde 1886.

Uso:
    python scripts/ingest_suin_juriscol.py
    python scripts/ingest_suin_juriscol.py --tipo LEY        # solo leyes
    python scripts/ingest_suin_juriscol.py --batch-size 500  # default 1000
    python scripts/ingest_suin_juriscol.py --dry-run         # no INSERT

El script es idempotente: usa ON CONFLICT (citation_ref) DO UPDATE.
Si una norma ya está en leyes_normas con vigencia 'modulada' (seedeada por
nosotros manualmente), NO la sobreescribe — preserva la vigencia local más
informada.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from typing import Any, Optional

SUPABASE_REF = os.getenv("SUPABASE_REF", "osyrwsbruydcyhdjvjpv")
SUPABASE_TOKEN = os.getenv("SUPABASE_ACCESS_TOKEN")
if not SUPABASE_TOKEN:
    raise SystemExit("SUPABASE_ACCESS_TOKEN env var required")
SUPABASE_API = f"https://api.supabase.com/v1/projects/{SUPABASE_REF}/database/query"

DATASET_URL = "https://www.datos.gov.co/resource/fiev-nid6.json"

# Mapeo de tipos del dataset → tipo aceptado por leyes_normas.tipo
# (check constraint: LEY, DECRETO, RESOLUCION, ACUERDO, CIRCULAR, CODIGO,
#                    CONSTITUCION, OTRO)
TIPO_MAP = {
    "LEY": "LEY",
    "DECRETO": "DECRETO",
    "RESOLUCION": "RESOLUCION",
    "RESOLUCION EXTERNA": "RESOLUCION",
    "ACUERDO": "ACUERDO",
    "CIRCULAR": "CIRCULAR",
    "CIRCULAR EXTERNA": "CIRCULAR",
    "CIRCULAR CONJUNTA": "CIRCULAR",
    "CIRCULAR VICEPRESIDENCIAL": "CIRCULAR",
    "CARTA CIRCULAR": "CIRCULAR",
    "CODIGO": "CODIGO",
    "CONSTITUCION POLITICA": "CONSTITUCION",
    "ACTO LEGISLATIVO": "OTRO",
    "DIRECTIVA PRESIDENCIAL": "OTRO",
    "DIRECTIVA VICEPRESIDENCIAL": "OTRO",
    "DIRECTIVA MINISTERIAL": "OTRO",
    "INSTRUCCION": "OTRO",
    "INSTRUCCION ADMINISTRATIVA CONJUNTA": "OTRO",
}

# Mapeo de vigencia SUIN → vigencia leyes_normas
# (check constraint: vigente, derogada, modulada, suspendida, inexequible,
#                    desconocida)
VIGENCIA_MAP = {
    "Vigente": "vigente",
    "vigente": "vigente",
    "No vigente": "derogada",
    "no vigente": "derogada",
    "Derogada": "derogada",
    "derogada": "derogada",
    "Modificada": "modulada",
    "modificada": "modulada",
    "Suspendida": "suspendida",
    "suspendida": "suspendida",
    "Inexequible": "inexequible",
    "inexequible": "inexequible",
}


def fetch_page(offset: int, limit: int, tipo_filter: Optional[str] = None) -> list[dict]:
    """Pide una página al dataset Socrata."""
    params = {
        "$select": "tipo,n_mero,a_o,sector,vigencia,entidad,materia,art_culos",
        "$limit": str(limit),
        "$offset": str(offset),
        "$order": "a_o,n_mero",
    }
    if tipo_filter:
        params["$where"] = f"tipo='{tipo_filter}'"

    url = f"{DATASET_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} on offset={offset}: {e.read()[:200].decode('utf-8', errors='replace')}")
        return []
    except Exception as e:
        print(f"  fetch failed offset={offset}: {e}")
        return []


def normalize_row(row: dict) -> Optional[dict]:
    """Transforma fila SUIN al formato de leyes_normas."""
    tipo_raw = (row.get("tipo") or "").strip().upper()
    if not tipo_raw or tipo_raw == "NULL":
        return None
    tipo = TIPO_MAP.get(tipo_raw, "OTRO")

    numero_raw = (row.get("n_mero") or "").strip()
    if not numero_raw:
        return None

    anio_raw = (row.get("a_o") or "").strip()
    try:
        anio = int(anio_raw)
        if anio < 1800 or anio > 2100:
            return None
    except (TypeError, ValueError):
        return None

    vigencia = VIGENCIA_MAP.get(row.get("vigencia"), "vigente")

    # Citation ref canónica (debe coincidir con parse_citation_ref del verifier)
    label_map = {
        "LEY": "LEY", "DECRETO": "DECRETO", "RESOLUCION": "RESOLUCION",
        "ACUERDO": "ACUERDO", "CIRCULAR": "CIRCULAR", "CODIGO": "CODIGO",
        "CONSTITUCION": "CONSTITUCION", "OTRO": tipo_raw,
    }
    label = label_map.get(tipo, tipo)
    citation_ref = f"{label} {numero_raw}/{anio}"

    materia = row.get("materia") or ""
    entidad = row.get("entidad") or ""
    sector = row.get("sector") or ""
    arts_raw = row.get("art_culos")
    try:
        articulos_count = int(arts_raw) if arts_raw else None
    except (TypeError, ValueError):
        articulos_count = None

    titulo = f"{label} {numero_raw} DE {anio}"
    if entidad:
        titulo += f" · {entidad}"

    return {
        "tipo": tipo,
        "numero": numero_raw,
        "anio": anio,
        "citation_ref": citation_ref,
        "titulo": titulo[:400],
        "vigencia": vigencia,
        "entidad": entidad[:200] if entidad else None,
        "materia": materia[:400] if materia else None,
        "sector": sector[:100] if sector else None,
        "articulos_count": articulos_count,
        "fuente": "suin_juriscol",
        "fuente_url": f"https://www.suin-juriscol.gov.co/legislacion/normatividad.html",
    }


def supabase_query(sql: str) -> Any:
    """Ejecuta SQL via management API y devuelve resultado."""
    body = json.dumps({"query": sql}).encode("utf-8")
    req = urllib.request.Request(
        SUPABASE_API,
        method="POST",
        headers={
            "Authorization": f"Bearer {SUPABASE_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Accept": "application/json",
        },
        data=body,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"  Supabase HTTP {e.code}: {err_body[:300]}")
        return None


def insert_batch(rows: list[dict], dry_run: bool = False) -> int:
    """Insert un batch en leyes_normas via UPSERT.

    Estrategia ON CONFLICT (citation_ref):
      · Si la fila YA EXISTE con vigencia 'modulada' (seedeada manualmente),
        preservar esa vigencia (más informada que SUIN).
      · Si tiene fuente='senado' (verificada live), preservar el fuente_url
        y verified_at (porque tiene texto completo del articulado).
      · Solo update titulo/materia/entidad/articulos_count desde SUIN.
    """
    if not rows:
        return 0

    # Dedupe por citation_ref dentro del batch (SUIN puede repetir refs).
    # Preferir filas con vigencia != vigente (más informativas) o con materia.
    VIGENCIA_PRIO = {"inexequible": 5, "derogada": 4, "modulada": 3,
                     "suspendida": 2, "vigente": 1, "desconocida": 0}
    dedup: dict[str, dict] = {}
    for r in rows:
        key = r["citation_ref"]
        prev = dedup.get(key)
        if prev is None:
            dedup[key] = r
            continue
        # Comparar prioridad de vigencia
        if VIGENCIA_PRIO.get(r["vigencia"], 0) > VIGENCIA_PRIO.get(prev["vigencia"], 0):
            dedup[key] = r
        elif r.get("materia") and not prev.get("materia"):
            dedup[key] = r
    rows = list(dedup.values())

    if dry_run:
        return len(rows)

    values_parts = []
    for r in rows:
        # Escape simple para SQL · solo strings van con quote, ints van raw
        def q(v):
            if v is None:
                return "NULL"
            if isinstance(v, (int, float)):
                return str(v)
            s = str(v).replace("'", "''")
            return f"'{s}'"
        values_parts.append(
            f"({q(r['tipo'])}, {q(r['numero'])}, {q(r['anio'])}, "
            f"{q(r['citation_ref'])}, {q(r['titulo'])}, {q(r['vigencia'])}, "
            f"{q(r['entidad'])}, {q(r['materia'])}, {q(r['sector'])}, "
            f"{q(r['articulos_count'])}, {q(r['fuente'])}, {q(r['fuente_url'])})"
        )

    sql = f"""
    insert into leyes_normas
      (tipo, numero, anio, citation_ref, titulo, vigencia,
       entidad, materia, sector, articulos_count, fuente, fuente_url)
    values
      {', '.join(values_parts)}
    on conflict (citation_ref) do update set
      titulo = case
        when leyes_normas.fuente in ('senado', 'manual') then leyes_normas.titulo
        else excluded.titulo
      end,
      vigencia = case
        when leyes_normas.vigencia in ('modulada', 'inexequible')
             and leyes_normas.fuente in ('manual', 'senado') then leyes_normas.vigencia
        else excluded.vigencia
      end,
      entidad = coalesce(leyes_normas.entidad, excluded.entidad),
      materia = coalesce(leyes_normas.materia, excluded.materia),
      sector = coalesce(leyes_normas.sector, excluded.sector),
      articulos_count = coalesce(leyes_normas.articulos_count, excluded.articulos_count),
      updated_at = now()
    ;
    """
    result = supabase_query(sql)
    if result is None:
        return 0
    if isinstance(result, dict) and result.get("message", "").startswith("Failed"):
        print(f"  SQL error: {result.get('message')[:200]}")
        return 0
    return len(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tipo", help="Solo importar este tipo (LEY, DECRETO, etc.)")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--max-pages", type=int, default=1000,
                        help="Detenerse después de N páginas (debug)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print(f"\n[L11] Bulk ingest SUIN-Juriscol -> leyes_normas")
    print(f"  Dataset: fiev-nid6 ({DATASET_URL})")
    print(f"  Batch:   {args.batch_size} rows/page")
    if args.tipo:
        print(f"  Filtro:  tipo='{args.tipo}'")
    if args.dry_run:
        print(f"  DRY RUN · sin INSERTs reales\n")
    print()

    started = time.time()
    total_fetched = 0
    total_inserted = 0
    page = 0
    skipped_no_normalize = 0

    while page < args.max_pages:
        offset = page * args.batch_size
        rows_raw = fetch_page(offset, args.batch_size, args.tipo)
        if not rows_raw:
            break
        page += 1
        total_fetched += len(rows_raw)

        rows_norm = []
        for r in rows_raw:
            n = normalize_row(r)
            if n is None:
                skipped_no_normalize += 1
                continue
            rows_norm.append(n)

        if rows_norm:
            inserted = insert_batch(rows_norm, dry_run=args.dry_run)
            total_inserted += inserted

        elapsed = time.time() - started
        rate = total_fetched / elapsed if elapsed > 0 else 0
        print(
            f"  page {page:3d} | offset {offset:6d} | "
            f"fetched {len(rows_raw):4d} | normalized {len(rows_norm):4d} | "
            f"total inserted {total_inserted:6d} | "
            f"{elapsed:5.1f}s | {rate:5.0f} rec/s"
        )

        if len(rows_raw) < args.batch_size:
            print(f"\n  end of dataset reached at page {page}")
            break

        # Cortesía con el servidor
        time.sleep(0.3)

    elapsed = time.time() - started
    print(f"\n[L11] DONE")
    print(f"  Total fetched:    {total_fetched}")
    print(f"  Total normalized: {total_fetched - skipped_no_normalize}")
    print(f"  Total inserted:   {total_inserted}")
    print(f"  Skipped (bad):    {skipped_no_normalize}")
    print(f"  Elapsed:          {elapsed:.1f}s ({elapsed/60:.1f}min)")

    # Verificación final
    if not args.dry_run:
        print(f"\n[L11] Verifying database state...")
        r = supabase_query("select count(*) as total, count(distinct citation_ref) as unique_refs from leyes_normas;")
        if r and isinstance(r, list) and r:
            print(f"  leyes_normas: {r[0]}")
        r = supabase_query("select vigencia, count(*) from leyes_normas group by vigencia order by count(*) desc;")
        if r and isinstance(r, list):
            print(f"  Vigencia breakdown:")
            for row in r:
                print(f"    {row.get('vigencia','?'):15} {row.get('count',0)}")


if __name__ == "__main__":
    main()
