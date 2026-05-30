"""Sprint M20.08 · Sandbox runner con bubblewrap (Linux) o fallback restringido.

Diseño:
  1. El Brain pide ejecutar código Python que recibió como output de un LLM.
  2. `run_python_in_sandbox(code, ...)` crea un dir temporal aislado,
     escribe el código + harness, lo invoca via bubblewrap, captura stdout/
     stderr/files, retorna SandboxResult.
  3. Network: solo via unix socket → proxy externo con allowlist.
  4. Filesystem: read-only en TODO el filesystem excepto el tmp del sandbox.

SECURITY NOTES:
  - bubblewrap es la ÚNICA opción aceptable en producción
  - subprocess fallback NO es seguro, solo para desarrollo local
  - allowlist de network OBLIGATORIA en producción
  - timeout máximo 60s
  - límite de memoria via cgroups si bubblewrap lo soporta
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)


# ---- Allowlist DNS hosts ----

DEFAULT_NETWORK_ALLOWLIST = frozenset([
    "suin-juriscol.gov.co",
    "corteconstitucional.gov.co",
    "cortesuprema.gov.co",
    "secretariasenado.gov.co",
    "funcionpublica.gov.co",
    "datos.gov.co",
    "banrep.gov.co",
    "dane.gov.co",
])


# Python builtins/módulos BLOQUEADOS (lista negra defensiva)
BLOCKED_IMPORTS = frozenset([
    "os",         # bloqueamos por defecto, código permitido NO debe usar os
    "sys",        # idem
    "subprocess", # ejecución de procesos externos
    "socket",     # network raw
    "ctypes",     # FFI inseguro
    "multiprocessing",
    "threading",
])

ALLOWED_IMPORTS = frozenset([
    "json", "math", "datetime", "decimal", "statistics", "re",
    "collections", "itertools", "functools",
    "httpx",       # solo via proxy
    "requests",    # solo via proxy
    "asyncio",
    "openpyxl", "python_docx",   # archivos legales
    "pandas", "numpy",            # análisis numérico legal
])


@dataclass
class SandboxConfig:
    timeout_seconds: int = 30
    max_memory_mb: int = 512
    network_allowlist: frozenset[str] = field(default_factory=lambda: DEFAULT_NETWORK_ALLOWLIST)
    allow_network: bool = False        # default: SIN red. Brain debe pedirla explícitamente.
    workdir_prefix: str = "/tmp/lexai-sandbox"


@dataclass
class SandboxResult:
    success: bool
    exit_code: Optional[int]
    stdout: str
    stderr: str
    duration_ms: int
    files_created: list[str] = field(default_factory=list)
    bytes_read_network: int = 0
    error: Optional[str] = None
    backend_used: str = ""


class SandboxError(Exception):
    """Errores en sandbox setup o ejecución."""


# ---- Detección backend ----

def is_bubblewrap_available() -> bool:
    """True si bubblewrap (bwrap) está disponible y ejecutable."""
    return shutil.which("bwrap") is not None


def _validate_code_imports(code: str) -> Optional[str]:
    """Análisis estático mínimo: rechaza imports en blocklist.

    NO es seguridad real (un atacante puede bypassear con __import__), solo
    sirve para detectar errores honestos del LLM. La seguridad real viene
    del aislamiento bubblewrap + network proxy.
    """
    import re
    for line in code.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"(?:from\s+(\S+)\s+import|import\s+(\S+))", line)
        if not m:
            continue
        mod = (m.group(1) or m.group(2) or "").split(".")[0].split(",")[0].strip()
        if mod in BLOCKED_IMPORTS:
            return f"import bloqueado: {mod} (en blocklist)"
    return None


# ---- Runner principal ----

async def run_python_in_sandbox(
    code: str,
    *,
    generation_id: Optional[UUID] = None,
    config: Optional[SandboxConfig] = None,
    input_data: Optional[dict] = None,
) -> SandboxResult:
    """Ejecuta `code` (Python) en sandbox aislado y retorna SandboxResult.

    El código recibe `input_data` como variable `INPUT` (dict) si se provee.
    Debe escribir su resultado en stdout como JSON (last line).
    """
    config = config or SandboxConfig()
    generation_id = generation_id or uuid4()

    # 1) Validación estática
    blocked = _validate_code_imports(code)
    if blocked:
        return SandboxResult(
            success=False, exit_code=None, stdout="", stderr=blocked,
            duration_ms=0, error="static_validation_failed", backend_used="none",
        )

    # 2) Setup dir aislado
    workdir = Path(tempfile.mkdtemp(prefix=f"{config.workdir_prefix.replace('/tmp/', '')}-"))
    code_path = workdir / "user_code.py"
    output_path = workdir / "output.json"

    harness = _build_harness(input_data)
    code_path.write_text(harness + "\n\n" + code, encoding="utf-8")

    # 3) Ejecutar
    started = time.perf_counter()
    if is_bubblewrap_available():
        result = await _run_with_bubblewrap(code_path, workdir, config)
    else:
        result = await _run_with_subprocess_fallback(code_path, workdir, config)
    result.duration_ms = int((time.perf_counter() - started) * 1000)

    # 4) Recolectar archivos creados
    try:
        result.files_created = [
            str(p.relative_to(workdir))
            for p in workdir.rglob("*") if p.is_file() and p.name != "user_code.py"
        ]
    except Exception:
        pass

    # 5) Cleanup
    try:
        shutil.rmtree(workdir, ignore_errors=True)
    except Exception:
        pass

    return result


def _build_harness(input_data: Optional[dict]) -> str:
    """Inyecta INPUT como variable global accesible desde el código user."""
    if input_data is None:
        return "INPUT = {}"
    data_json = json.dumps(input_data, ensure_ascii=False, default=str)
    return f"INPUT = {data_json!r}\nimport json as _json\nINPUT = _json.loads(INPUT)"


async def _run_with_bubblewrap(
    code_path: Path, workdir: Path, config: SandboxConfig,
) -> SandboxResult:
    """Ejecuta con bwrap: filesystem read-only excepto workdir; network controlado."""
    args = [
        "bwrap",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",
        "--proc", "/proc",
        "--dev", "/dev",
        "--bind", str(workdir), "/workdir",
        "--chdir", "/workdir",
        "--die-with-parent",
        "--new-session",
        "--cap-drop", "all",
    ]
    if not config.allow_network:
        args += ["--unshare-net"]
    args += [sys.executable, "/workdir/user_code.py"]

    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=config.timeout_seconds + 5,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=config.timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return SandboxResult(
                success=False, exit_code=None,
                stdout="", stderr=f"timeout {config.timeout_seconds}s",
                duration_ms=0, error="timeout", backend_used="bubblewrap",
            )
        return SandboxResult(
            success=proc.returncode == 0,
            exit_code=proc.returncode,
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            duration_ms=0,
            backend_used="bubblewrap",
        )
    except FileNotFoundError:
        return SandboxResult(
            success=False, exit_code=None, stdout="", stderr="bubblewrap no encontrado",
            duration_ms=0, error="bubblewrap_missing", backend_used="bubblewrap",
        )


async def _run_with_subprocess_fallback(
    code_path: Path, workdir: Path, config: SandboxConfig,
) -> SandboxResult:
    """FALLBACK NO-SEGURO solo para desarrollo (Windows/macOS sin bwrap).

    NO usar en producción. El código corre con los mismos permisos del proceso.
    """
    logger.warning("Sandbox usando subprocess fallback (NO SEGURO). "
                   "Instala bubblewrap en producción.")
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(code_path),
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=config.timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return SandboxResult(
                success=False, exit_code=None,
                stdout="", stderr=f"timeout {config.timeout_seconds}s",
                duration_ms=0, error="timeout", backend_used="subprocess_fallback",
            )
        return SandboxResult(
            success=proc.returncode == 0,
            exit_code=proc.returncode,
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            duration_ms=0,
            backend_used="subprocess_fallback",
        )
    except Exception as e:
        return SandboxResult(
            success=False, exit_code=None, stdout="", stderr=str(e)[:300],
            duration_ms=0, error=f"subprocess_error: {e}", backend_used="subprocess_fallback",
        )
