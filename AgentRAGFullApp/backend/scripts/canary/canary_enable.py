"""Sprint M20.05 · S4.1 · Habilita canary para 1 firm QA.

Setea LEAN_ORCHESTRATOR_FIRMS=<uuid> en Railway prod via GraphQL.
Si la mutation está bloqueada por classifier, imprime instrucciones para
hacerlo manualmente en Railway UI.

USO:
  python scripts/canary/canary_enable.py <firm_uuid>
  python scripts/canary/canary_enable.py <firm_uuid> --add  # añadir a allowlist existente
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

_BACKEND_ROOT = Path(__file__).parent.parent.parent


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(_BACKEND_ROOT / ".env")
_load_env(_BACKEND_ROOT.parent.parent.parent / "Legal Demo" / "Legal_agent_Frontend" / ".env.local")


# Railway IDs (de la memoria project_lexai_railway)
RAILWAY_PROJECT_ID = "2eb02ad0-a7ad-40e9-ba64-92a9a7a309fd"
RAILWAY_ENV_ID = "68f00e48-e699-4922-971c-e2adbde492c4"
RAILWAY_SERVICE_ID = "3d5eb807-196a-4cbb-98e7-51f83ed68d42"   # legal-agent-backend
GRAPHQL_URL = "https://backboard.railway.com/graphql/v2"


def _gql(query: str, token: str) -> dict:
    body = json.dumps({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        GRAPHQL_URL, data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}"}
    except Exception as e:
        return {"error": str(e)[:300]}


def get_current_allowlist(token: str) -> str | None:
    query = f'''
    {{ variables(
        projectId: "{RAILWAY_PROJECT_ID}"
        environmentId: "{RAILWAY_ENV_ID}"
        serviceId: "{RAILWAY_SERVICE_ID}"
    ) }}
    '''
    res = _gql(query, token)
    if "error" in res:
        print(f"WARN · no pude leer vars actuales: {res['error']}", file=sys.stderr)
        return None
    vars_dict = res.get("data", {}).get("variables", {}) or {}
    return vars_dict.get("LEAN_ORCHESTRATOR_FIRMS", "")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("firm_uuid", help="UUID de la firm QA a habilitar")
    p.add_argument("--add", action="store_true", help="Añadir a allowlist existente (no reemplazar)")
    p.add_argument("--token", help="Railway token (override RAILWAY_API_TOKEN env)")
    args = p.parse_args()

    token = args.token or os.getenv("RAILWAY_API_TOKEN")
    if not token:
        print("ERROR: define RAILWAY_API_TOKEN en .env o pasa --token", file=sys.stderr)
        return 1

    firm_id = args.firm_uuid.strip().lower()
    # Validar UUID superficialmente
    if len(firm_id) != 36 or firm_id.count("-") != 4:
        print(f"ERROR: '{firm_id}' no parece un UUID v4 (debe tener 36 chars con 4 guiones)")
        return 1

    current = get_current_allowlist(token) if args.add else ""
    if current:
        existing = {x.strip().lower() for x in current.split(",") if x.strip()}
        existing.add(firm_id)
        new_value = ",".join(sorted(existing))
    else:
        new_value = firm_id

    print(f"\n=== Habilitando canary para firm {firm_id} ===")
    print(f"  Nueva allowlist: {new_value}")
    print()
    print("⚠ La mutation variableUpsert está bloqueada por classifier por defecto.")
    print("Para aplicar el cambio, ABRE Railway UI:")
    print(f"  https://railway.app/project/{RAILWAY_PROJECT_ID}/service/{RAILWAY_SERVICE_ID}/variables?environmentId={RAILWAY_ENV_ID}")
    print()
    print("Y setea estas variables:")
    print(f"  LEAN_ORCHESTRATOR_FIRMS = {new_value}")
    print(f"  USE_LEAN_ORCHESTRATOR = false   (mantén false; la allowlist tiene prioridad para esta firm)")
    print(f"  LEAN_ORCHESTRATOR_PERCENTAGE = 0   (sin rollout porcentual aún)")
    print()
    print("Luego trigger redeploy:")
    print(f"  curl -X POST {GRAPHQL_URL} -H 'Authorization: Bearer <token>' \\")
    print(f"    -H 'Content-Type: application/json' \\")
    print(f"    -d '{{\"query\":\"mutation {{ serviceInstanceDeployV2(serviceId:\\\"{RAILWAY_SERVICE_ID}\\\", environmentId:\\\"{RAILWAY_ENV_ID}\\\") }}\"}}'")
    print()
    print("Cuando el deploy esté verde:")
    print(f"  python scripts/canary/canary_smoke.py {firm_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
