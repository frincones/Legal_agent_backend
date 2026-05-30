"""Sprint M20.08 · Sandbox runtime para ejecución segura de código Python.

Permite al Brain ejecutar código Python arbitrario (cálculos legales
complejos, scrapers puntuales) en un proceso aislado con:
  - Filesystem restringido a /tmp/sandbox-{generation_id}
  - Network egress controlado vía proxy con allowlist
  - Timeout configurable (default 30s)
  - Memory limit (default 512MB)
  - Audit completo en `sandbox_execution_log`

Backends soportados:
  - bubblewrap (Linux nativo, en Railway containers)
  - subprocess restringido (fallback Windows/macOS para desarrollo)

NO se usa en producción sin haber pasado tests de seguridad
(`tests/sandbox/test_security.py`).
"""

from .runner import (
    SandboxResult,
    SandboxConfig,
    SandboxError,
    run_python_in_sandbox,
    is_bubblewrap_available,
)

__all__ = [
    "SandboxResult",
    "SandboxConfig",
    "SandboxError",
    "run_python_in_sandbox",
    "is_bubblewrap_available",
]
