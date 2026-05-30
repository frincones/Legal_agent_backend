"""Sprint M20.01 · Feature flags para el rollout gradual del LeanOrchestrator.

El nuevo orchestrator (ReAct + 18 tools) coexiste con el legacy (17 stages)
durante S2-S6. Esta capa decide qué path tomar por request.

Estrategia de rollout (configurable vía env vars):

  USE_LEAN_ORCHESTRATOR=false  → 100% legacy (default, sin riesgo)
  USE_LEAN_ORCHESTRATOR=true   → 100% lean (post S6, rollout completo)

  LEAN_ORCHESTRATOR_FIRMS=uuid1,uuid2  → allowlist canary (S4)
  LEAN_ORCHESTRATOR_PERCENTAGE=10      → hash-based percentage (S5)

Precedencia:
  1. Si USE_LEAN_ORCHESTRATOR=true → True (override total)
  2. Si firm_id en allowlist → True
  3. Si hash(firm_id) % 100 < percentage → True
  4. Default → False (legacy)

Por qué hash sobre firm_id y NO sobre generation_id:
  - Una firm que cae en arm "lean" debe permanecer consistentemente en lean
    durante toda la rampa (mejor para feedback humano + comparativas).
  - Si fuera por generation_id, una misma firm podría obtener resultados
    inconsistentes entre generaciones, complicando el A/B testing.
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Optional
from uuid import UUID

log = logging.getLogger(__name__)

# ---- cache de variables (re-leídas en cada llamada para hot-reload) ----

def _env_bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("true", "1", "yes", "on")


def _env_set(name: str) -> set[str]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return set()
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _env_int(name: str, default: int = 0) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return max(0, min(100, int(raw)))
    except ValueError:
        log.warning("env %s no es int válido: %r — default %d", name, raw, default)
        return default


def _hash_firm_to_bucket(firm_id: UUID | str) -> int:
    """Determinístico: misma firm_id siempre cae en mismo bucket 0-99."""
    h = hashlib.sha256(str(firm_id).encode("utf-8")).hexdigest()
    return int(h[:8], 16) % 100


def should_use_lean(
    firm_id: Optional[UUID | str] = None,
    generation_id: Optional[UUID | str] = None,
) -> bool:
    """Decide si esta request usa el LeanOrchestrator nuevo o el legacy.

    Args:
        firm_id: identifica el tenant. Usado para allowlist + hash-bucket.
        generation_id: solo para logging/debugging, NO afecta la decisión.

    Returns:
        True  → routea a LeanOrchestrator
        False → routea al Orchestrator legacy (17 stages)
    """
    # 1) override total
    if _env_bool("USE_LEAN_ORCHESTRATOR", False):
        log.debug("feature_flag: USE_LEAN_ORCHESTRATOR=true → lean (gen=%s)", generation_id)
        return True

    # sin firm_id → siempre legacy (no podemos hashear ni mirar allowlist)
    if not firm_id:
        return False

    firm_str = str(firm_id).lower()

    # 2) allowlist canary
    firms_allow = _env_set("LEAN_ORCHESTRATOR_FIRMS")
    if firm_str in firms_allow:
        log.info("feature_flag: firm=%s en allowlist → lean", firm_str)
        return True

    # 3) hash-based percentage
    percentage = _env_int("LEAN_ORCHESTRATOR_PERCENTAGE", 0)
    if percentage <= 0:
        return False
    if percentage >= 100:
        return True

    bucket = _hash_firm_to_bucket(firm_id)
    chosen = bucket < percentage
    if chosen:
        log.info(
            "feature_flag: firm=%s bucket=%d < %d%% → lean",
            firm_str, bucket, percentage,
        )
    return chosen


def orchestrator_kind(firm_id: Optional[UUID | str] = None,
                      generation_id: Optional[UUID | str] = None) -> str:
    """Helper para logging y para persistir en generation_audit.orchestrator_kind."""
    return "lean" if should_use_lean(firm_id, generation_id) else "legacy"


# ---- diagnóstico (útil para healthcheck/admin endpoint) ----

def flags_snapshot() -> dict:
    """Snapshot del estado actual de las flags. Útil para /admin/feature-flags."""
    return {
        "USE_LEAN_ORCHESTRATOR": _env_bool("USE_LEAN_ORCHESTRATOR", False),
        "LEAN_ORCHESTRATOR_FIRMS": sorted(_env_set("LEAN_ORCHESTRATOR_FIRMS")),
        "LEAN_ORCHESTRATOR_PERCENTAGE": _env_int("LEAN_ORCHESTRATOR_PERCENTAGE", 0),
    }
