"""Sprint L10 - Ingest centros conciliacion -> centros_conciliacion.

Fuente: https://www.datos.gov.co/resource/7p9a-zd9k.json (~428 centros)
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
DATASET_URL = "https://www.datos.gov.co/resource/7p9a-zd9k.json"


def fetch_all() -> list[dict]:
    params = {
        "$select": "nombre,clase_centro,ciudad,departamento,direcci_n_centro,tel_fono_centro,correo_centro",
        "$limit": "1000",
        "$order": "departamento,ciudad,nombre",
    }
    url = f"{DATASET_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def q(v):
    if v is None or v == "" or (isinstance(v, str) and v.lower() in ("null", "sin registro")):
        return "NULL"
    s = str(v).replace("'", "''")
    return f"'{s}'"


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


def main():
    print("[L10] Ingest centros conciliacion -> centros_conciliacion")
    rows = fetch_all()
    print(f"  fetched {len(rows)} centros from datos.gov.co/7p9a-zd9k")

    # Dedup por (nombre, ciudad) en el batch
    seen = {}
    for r in rows:
        key = (r.get("nombre", "").strip(), r.get("ciudad", "").strip())
        if key[0]:
            seen[key] = r
    rows_dedup = list(seen.values())

    print(f"  after dedup: {len(rows_dedup)}")

    # Insert en chunks de 100
    chunks = [rows_dedup[i:i+100] for i in range(0, len(rows_dedup), 100)]
    total_inserted = 0
    for ci, chunk in enumerate(chunks):
        values_parts = []
        for r in chunk:
            nombre = r.get("nombre", "").strip()
            ciudad = r.get("ciudad", "").strip()
            departamento = r.get("departamento", "").strip()
            entidad = r.get("clase_centro", "").strip()
            direccion = r.get("direcci_n_centro", "").strip()
            telefono = r.get("tel_fono_centro", "").strip()
            email = r.get("correo_centro", "").strip()
            # Normalize entidad
            entidad_short = entidad
            if "JUR" in entidad and "SIN" in entidad:
                entidad_short = "ONG/Sin animo de lucro"
            elif "C" in entidad and "MERCIO" in entidad.upper():
                entidad_short = "Camara de Comercio"
            elif "UNIVERSIDAD" in entidad.upper():
                entidad_short = "Universidad"
            elif "NOTAR" in entidad.upper():
                entidad_short = "Notaria"

            values_parts.append(
                f"({q(nombre)}, {q(entidad_short)}, {q(ciudad)}, {q(departamento)}, "
                f"{q(direccion)}, {q(telefono)}, {q(email)}, 'autorizado', "
                f"'sicaac_datos_gov', 'https://sicaac.minjusticia.gov.co')"
            )
        sql = f"""
        insert into centros_conciliacion
          (nombre, entidad_origen, ciudad, departamento,
           direccion, telefono, email, estado, fuente, url_oficial)
        values
          {', '.join(values_parts)}
        on conflict (nombre, ciudad) do update set
          entidad_origen = excluded.entidad_origen,
          direccion = excluded.direccion,
          telefono = excluded.telefono,
          email = excluded.email,
          estado = excluded.estado,
          updated_at = now()
        ;
        """
        res = supabase_query(sql)
        if res is not None:
            total_inserted += len(chunk)
        print(f"  chunk {ci+1}/{len(chunks)}: {len(chunk)} rows | total inserted {total_inserted}")
        time.sleep(0.2)

    print(f"\n[L10] DONE")
    r = supabase_query("select count(*) as total, count(distinct ciudad) as ciudades from centros_conciliacion;")
    print(f"  centros_conciliacion: {r}")
    r = supabase_query("select departamento, count(*) from centros_conciliacion group by departamento order by count(*) desc limit 10;")
    if r:
        print("  Top 10 departamentos:")
        for row in r:
            print(f"    {row.get('departamento','?'):30} {row.get('count',0):5d}")


if __name__ == "__main__":
    main()
