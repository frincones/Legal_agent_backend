"""Sprint M20.06 · S5.1 · Set LEAN_ORCHESTRATOR_PERCENTAGE.

Cambia el porcentaje de tráfico routeado a lean en Railway prod.

USO:
  python scripts/rollout/set_percentage.py 10       # rampa a 10%
  python scripts/rollout/set_percentage.py 50       # rampa a 50%
  python scripts/rollout/set_percentage.py 100      # rollout completo
  python scripts/rollout/set_percentage.py 0        # rollback

Como variableUpsert puede estar bloqueada por classifier, el script imprime
las instrucciones manuales si no puede aplicar la mutation directamente.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
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


RAILWAY_PROJECT_ID = "2eb02ad0-a7ad-40e9-ba64-92a9a7a309fd"
RAILWAY_ENV_ID = "68f00e48-e699-4922-971c-e2adbde492c4"
RAILWAY_SERVICE_ID = "3d5eb807-196a-4cbb-98e7-51f83ed68d42"


def apply_mutation(pct: int, token: str) -> bool:
    mutation = f'''
mutation {{
  variableUpsert(input: {{
    projectId: "{RAILWAY_PROJECT_ID}"
    environmentId: "{RAILWAY_ENV_ID}"
    serviceId: "{RAILWAY_SERVICE_ID}"
    name: "LEAN_ORCHESTRATOR_PERCENTAGE"
    value: "{pct}"
  }})
}}'''
    body = json.dumps({"query": mutation}).encode("utf-8")
    req = urllib.request.Request(
        "https://backboard.railway.com/graphql/v2",
        data=body, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
            if "errors" in resp:
                print(f"ERROR mutation: {resp['errors']}", file=sys.stderr)
                return False
            print(f"OK · {resp}")
            return True
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return False


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("percentage", type=int)
    p.add_argument("--apply", action="store_true",
                    help="Aplica via GraphQL (puede ser bloqueado por classifier)")
    args = p.parse_args()

    if not (0 <= args.percentage <= 100):
        print(f"ERROR: percentage debe estar entre 0 y 100, dado {args.percentage}", file=sys.stderr)
        return 1

    print(f"\n=== Set LEAN_ORCHESTRATOR_PERCENTAGE={args.percentage} ===\n")

    token = os.getenv("RAILWAY_API_TOKEN")
    if args.apply and token:
        ok = apply_mutation(args.percentage, token)
        if ok:
            print(f"\n✓ Variable actualizada. Trigger redeploy si Railway no hace hot-reload:")
            print(f"  curl -X POST https://backboard.railway.com/graphql/v2 \\")
            print(f"    -H 'Authorization: Bearer $RAILWAY_API_TOKEN' \\")
            print(f"    -d '{{\"query\":\"mutation {{ serviceInstanceDeployV2(serviceId:\\\"{RAILWAY_SERVICE_ID}\\\", environmentId:\\\"{RAILWAY_ENV_ID}\\\") }}\"}}'")
            return 0
        return 2

    print("Para aplicar manualmente en Railway UI:")
    print(f"  https://railway.app/project/{RAILWAY_PROJECT_ID}/service/{RAILWAY_SERVICE_ID}/variables?environmentId={RAILWAY_ENV_ID}")
    print()
    print(f"  LEAN_ORCHESTRATOR_PERCENTAGE = {args.percentage}")
    print()
    print(f"  → con percentage > 0, hash(firm_id) % 100 < {args.percentage} routea a lean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
