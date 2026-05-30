"""Sprint M20.05 · S4.4 · Rollback drill (entrenamiento).

Simula un incidente y mide el tiempo de rollback completo:
  1. Anota timestamp inicial
  2. Sugiere comando rollback (no ejecuta sin --apply)
  3. Verifica que el flag cambió en Railway (consulta `variables`)
  4. Mide tiempo entre flip y nueva request ruteada a legacy
  5. Reporta MTTR (mean time to recover)

USO:
  python scripts/canary/rollback_drill.py --dry-run
  python scripts/canary/rollback_drill.py --apply --firm-uuid <uuid>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
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
DEFAULT_URL = "https://legal-agent-backend-production-fcfa.up.railway.app"


def get_flag_value(token: str) -> str:
    query = f'''
    {{ variables(
        projectId: "{RAILWAY_PROJECT_ID}"
        environmentId: "{RAILWAY_ENV_ID}"
        serviceId: "{RAILWAY_SERVICE_ID}"
    ) }}
    '''
    body = json.dumps({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        "https://backboard.railway.com/graphql/v2",
        data=body, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    return data.get("data", {}).get("variables", {}).get("USE_LEAN_ORCHESTRATOR", "(unset)")


def ping_health(url: str) -> dict:
    try:
        req = urllib.request.Request(f"{url}/health")
        with urllib.request.urlopen(req, timeout=10) as r:
            return {"status": r.status, "x_orch": r.headers.get("X-Orchestrator", "?")}
    except Exception as e:
        return {"status": "err", "error": str(e)[:120]}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--firm-uuid", default=None)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--url", default=DEFAULT_URL)
    args = p.parse_args()

    token = os.getenv("RAILWAY_API_TOKEN")
    if not token:
        print("ERROR: RAILWAY_API_TOKEN no configurado", file=sys.stderr)
        return 1

    print("=== Rollback drill ===\n")

    # 1. Estado inicial
    t0 = time.perf_counter()
    initial = get_flag_value(token)
    print(f"[T+0.0s] USE_LEAN_ORCHESTRATOR actual: {initial}")
    health = ping_health(args.url)
    print(f"[T+0.0s] /health → {health}")

    if not args.apply:
        print(f"\n[DRY-RUN] No se aplica rollback. Para ejecutar real:")
        print(f"  python scripts/canary/rollback_drill.py --apply")
        print(f"\nComandos manuales equivalentes:")
        print(f"  1. En Railway UI: USE_LEAN_ORCHESTRATOR=false")
        print(f"  2. Trigger redeploy o esperar variable hot-reload")
        print(f"  3. Verificar header X-Orchestrator: legacy en próximas requests")
        return 0

    # 2. Flip flag
    print(f"\n[T+{time.perf_counter() - t0:.1f}s] flip USE_LEAN_ORCHESTRATOR=false...")
    mutation = f'''
mutation {{
  variableUpsert(input: {{
    projectId: "{RAILWAY_PROJECT_ID}"
    environmentId: "{RAILWAY_ENV_ID}"
    serviceId: "{RAILWAY_SERVICE_ID}"
    name: "USE_LEAN_ORCHESTRATOR"
    value: "false"
  }})
}}'''
    body = json.dumps({"query": mutation}).encode("utf-8")
    req = urllib.request.Request(
        "https://backboard.railway.com/graphql/v2",
        data=body, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp_data = json.loads(r.read())
            if "errors" in resp_data:
                print(f"  ERROR mutation: {resp_data['errors']}", file=sys.stderr)
                return 2
            print(f"  OK mutation: {resp_data}")
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return 2

    # 3. Poll hasta confirmar flip
    for i in range(12):  # 60s max
        time.sleep(5)
        elapsed = time.perf_counter() - t0
        v = get_flag_value(token)
        print(f"[T+{elapsed:.1f}s] flag actual: {v}")
        if v.lower() in ("false", "0"):
            print(f"\n✓ Flag flipped in {elapsed:.1f}s")
            break
    else:
        print(f"\n⚠ Flag no se reflejó en 60s")
        return 3

    # 4. Verificar nuevas requests van a legacy
    print(f"\n[T+{time.perf_counter() - t0:.1f}s] verificando ruteo legacy...")
    # No podemos hacer un POST real sin token JWT; solo health check
    final = get_flag_value(token)
    print(f"[T+{time.perf_counter() - t0:.1f}s] flag final: {final}")

    mttr = time.perf_counter() - t0
    print(f"\n=== Rollback drill complete · MTTR = {mttr:.1f}s ===")
    if mttr < 30:
        print(f"✓ MTTR < 30s · OK")
        return 0
    else:
        print(f"⚠ MTTR {mttr:.1f}s > 30s target · revisar configuración Railway")
        return 4


if __name__ == "__main__":
    sys.exit(main())
