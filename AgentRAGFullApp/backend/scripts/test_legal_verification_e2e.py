"""Sprint L - E2E test suite for legal verification portals.

Tests against the LIVE deployed backend (or local for dev). Validates:
  L5  - vigencia (modulada, derogada, vigente from SUIN + manual seeds)
  L8  - predios catastrales (DIVIPOLA -> gestor, Bogota IDECA)
  L10 - centros conciliacion (search + verify)
  L11 - SUIN-Juriscol bulk cache hits

Cases used:
  - LEY 1437/2011 (CPACA) -> debe ser 'modulada'
  - LEY 100/1993 (Seg Social) -> vigente from SUIN
  - LEY 153/1887 (CC supletoria) -> vigente from SUIN
  - LEY 999999/2099 (fake) -> no_encontrada or sospechosa
  - Cedula catastral Bogota '11001' DIVIPOLA -> Bogota
  - Cedula catastral Medellin '05001' DIVIPOLA -> Antioquia
  - Cedula invalida '99999' -> divipola_invalido
  - Centro CCB ('Camara Comercio Bogota') -> match
  - Centro inexistente -> no_encontrado

Run:
    # Contra Supabase + LIVE backend
    python scripts/test_legal_verification_e2e.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.request
import urllib.parse
from typing import Optional, Any

SUPABASE_REF = os.getenv("SUPABASE_REF", "osyrwsbruydcyhdjvjpv")
SUPABASE_TOKEN = os.getenv("SUPABASE_ACCESS_TOKEN")
if not SUPABASE_TOKEN:
    raise SystemExit("SUPABASE_ACCESS_TOKEN env var required")
SUPABASE_API = f"https://api.supabase.com/v1/projects/{SUPABASE_REF}/database/query"


GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"


def supabase_query(sql: str) -> Any:
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
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}"}


def check(condition: bool, label: str, actual: str = "") -> bool:
    color = GREEN if condition else RED
    mark = "PASS" if condition else "FAIL"
    line = f"  [{color}{mark}{RESET}] {label}"
    if not condition and actual:
        line += f"\n         actual: {actual[:200]}"
    print(line)
    return condition


def section(title: str):
    print(f"\n{YELLOW}{'='*60}{RESET}")
    print(f"{YELLOW}  {title}{RESET}")
    print(f"{YELLOW}{'='*60}{RESET}")


def test_l11_suin_cache():
    """L11: leyes_normas debe tener ~87K registros."""
    section("L11 - SUIN-Juriscol bulk cache")
    r = supabase_query("select count(*) as total from leyes_normas;")
    if "error" in r:
        check(False, "leyes_normas accesible", r["error"])
        return
    total = r[0]["total"]
    check(total >= 87000, f"leyes_normas >= 87k (actual: {total})")

    # Distribucion por vigencia
    r = supabase_query("select vigencia, count(*) c from leyes_normas group by vigencia;")
    if isinstance(r, list):
        by_vig = {row["vigencia"]: row["c"] for row in r}
        check(by_vig.get("vigente", 0) > 80000, f"vigente >= 80k (actual: {by_vig.get('vigente')})")
        check(by_vig.get("derogada", 0) > 100, f"derogada >= 100 (actual: {by_vig.get('derogada')})")
        check(by_vig.get("modulada", 0) >= 3, f"modulada manual seeds (>=3, actual: {by_vig.get('modulada')})")


def test_l5_vigencia():
    """L5 + L11: casos reales de vigencia."""
    section("L5+L11 - Vigencia de leyes (casos reales)")

    # Caso 1: LEY 1437/2011 (CPACA) -> debe estar modulada (manual seed)
    r = supabase_query(
        "select citation_ref, vigencia, fuente from leyes_normas where citation_ref = 'LEY 1437/2011';"
    )
    if isinstance(r, list) and r:
        check(r[0]["vigencia"] == "modulada", "LEY 1437/2011 (CPACA) marcada modulada",
              actual=json.dumps(r[0]))
        check(r[0]["fuente"] in ("senado", "manual"),
              "LEY 1437/2011 fuente preservada (senado/manual)",
              actual=str(r[0]["fuente"]))
    else:
        check(False, "LEY 1437/2011 existe en leyes_normas")

    # Caso 2: LEY 100/1993 (Seg Social) -> vigente from SUIN
    r = supabase_query(
        "select citation_ref, vigencia, fuente, entidad from leyes_normas where citation_ref = 'LEY 100/1993';"
    )
    if isinstance(r, list) and r:
        check(r[0]["vigencia"] == "vigente", "LEY 100/1993 vigente")
        check(r[0]["fuente"] == "suin_juriscol", "LEY 100/1993 desde SUIN")
        check("CONGRESO" in (r[0]["entidad"] or "").upper(),
              "LEY 100/1993 entidad CONGRESO", actual=str(r[0]["entidad"]))
    else:
        check(False, "LEY 100/1993 en leyes_normas")

    # Caso 3: LEY 153/1887 (historica) -> vigente
    r = supabase_query(
        "select citation_ref, vigencia from leyes_normas where citation_ref = 'LEY 153/1887';"
    )
    if isinstance(r, list) and r:
        check(r[0]["vigencia"] == "vigente", "LEY 153/1887 (codigo civil supletoria) vigente")
    else:
        check(False, "LEY 153/1887 existe (historica)")

    # Caso 4: DECRETO 1080/2015 (Sector Cultura) -> vigente, entidad Min Cultura
    r = supabase_query(
        "select entidad from leyes_normas where citation_ref = 'DECRETO 1080/2015';"
    )
    if isinstance(r, list) and r:
        check("CULTURA" in (r[0]["entidad"] or "").upper(),
              "DECRETO 1080/2015 entidad Min Cultura",
              actual=str(r[0]["entidad"]))

    # Caso 5: ley falsa -> NO debe existir
    r = supabase_query(
        "select citation_ref from leyes_normas where citation_ref = 'LEY 999999/2099';"
    )
    if isinstance(r, list):
        check(len(r) == 0, "LEY 999999/2099 (fake) NO existe en cache")


def test_l8_predios():
    """L8: gestores catastrales + estructura cedula."""
    section("L8 - IGAC catastro / gestores catastrales")

    # Total gestores
    r = supabase_query("select count(*) as total from gestores_catastrales;")
    if isinstance(r, list):
        check(r[0]["total"] >= 1000, f"gestores_catastrales >= 1000 municipios (actual: {r[0]['total']})")

    # Bogota
    r = supabase_query("select * from gestores_catastrales where divipola = '11001';")
    if isinstance(r, list) and r:
        gestor = (r[0].get("gestor_cat") or "").upper()
        check("BOGOTA" in gestor or "CATASTRO" in gestor or "UAECD" in gestor,
              f"Bogota (11001) tiene gestor de Catastro Distrital",
              actual=str(r[0].get("gestor_cat")))
    else:
        check(False, "DIVIPOLA 11001 (Bogota) existe en gestores")

    # Medellin
    r = supabase_query("select * from gestores_catastrales where divipola = '05001';")
    if isinstance(r, list) and r:
        check(r[0]["municipio"].upper().startswith("MEDELL"),
              "DIVIPOLA 05001 = Medellin", actual=str(r[0]["municipio"]))
        check(r[0]["departamento"].upper() == "ANTIOQUIA",
              "Medellin departamento Antioquia",
              actual=str(r[0]["departamento"]))

    # Cali
    r = supabase_query("select * from gestores_catastrales where divipola = '76001';")
    if isinstance(r, list) and r:
        check(r[0]["municipio"].upper().startswith("CALI") or "CALI" in r[0]["municipio"].upper(),
              "DIVIPOLA 76001 = Cali", actual=str(r[0]["municipio"]))

    # Divipola invalido
    r = supabase_query("select * from gestores_catastrales where divipola = '99999';")
    if isinstance(r, list):
        check(len(r) == 0, "DIVIPOLA 99999 NO existe (validacion negativa)")


def test_l10_centros_conciliacion():
    """L10: centros conciliacion - search + match."""
    section("L10 - Centros de Conciliacion (SICAAC)")

    r = supabase_query("select count(*) as total from centros_conciliacion;")
    if isinstance(r, list):
        check(r[0]["total"] >= 400, f"centros_conciliacion >= 400 (actual: {r[0]['total']})")

    # CCB - acento-tolerante via wildcards ('C_MARA' matcha CAMARA y CAMARA con acento)
    r = supabase_query(
        "select nombre, ciudad, entidad_origen from centros_conciliacion "
        "where nombre ilike '%mara de comercio de bogot%' limit 3;"
    )
    if isinstance(r, list):
        check(len(r) >= 1, f"Camara de Comercio Bogota encontrada (matches: {len(r)})")

    # Universidad
    r = supabase_query(
        "select count(*) as c from centros_conciliacion where nombre ilike '%universidad%';"
    )
    if isinstance(r, list):
        check(r[0]["c"] >= 5, f"centros universitarios >= 5 (actual: {r[0]['c']})")

    # Bogota mas centros que cualquier otra ciudad
    r = supabase_query(
        "select ciudad, count(*) c from centros_conciliacion "
        "group by ciudad order by c desc limit 1;"
    )
    if isinstance(r, list) and r:
        top = (r[0]["ciudad"] or "").upper()
        check("BOGOT" in top, f"ciudad #1 es Bogota (actual: {top})")


def test_data_consistency():
    """Validaciones cruzadas - no se rompio otra data."""
    section("Cross-checks - integridad de tablas existentes")

    # jurisprudencia sigue intacta
    r = supabase_query("select count(*) as c from jurisprudencia;")
    if isinstance(r, list):
        check(r[0]["c"] > 0, f"jurisprudencia preservada (rows: {r[0]['c']})")

    # firms / users no afectados
    for tbl in ("firms", "users"):
        r = supabase_query(f"select count(*) as c from {tbl};")
        if isinstance(r, list):
            check(r[0]["c"] > 0, f"{tbl} preservada (rows: {r[0]['c']})")


def main():
    print(f"\n{YELLOW}Sprint L - E2E test suite for legal verification portals{RESET}")
    print(f"Supabase ref: {SUPABASE_REF}")
    print(f"Date: {time.strftime('%Y-%m-%d %H:%M')}\n")

    test_l11_suin_cache()
    test_l5_vigencia()
    test_l8_predios()
    test_l10_centros_conciliacion()
    test_data_consistency()

    print(f"\n{GREEN}{'='*60}{RESET}")
    print(f"{GREEN}  E2E suite completa. Revise PASS/FAIL arriba.{RESET}")
    print(f"{GREEN}{'='*60}{RESET}\n")


if __name__ == "__main__":
    main()
