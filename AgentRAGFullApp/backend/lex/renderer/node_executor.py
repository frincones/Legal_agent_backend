"""Sandbox para ejecutar JS docx-js@9.5 generado por Claude.

Contrato:
  - Input: código JS string + diccionario `data` con placeholders
  - Output: bytes del .docx generado, o NodeExecutionError con detalle

Seguridad:
  - subprocess separado con timeout duro (default 30s)
  - límite de memoria via ulimit (Linux/Docker)
  - NODE_PATH apunta a /opt/docx-runtime/node_modules (set en Dockerfile)
  - --no-warnings y --max-old-space-size acotan el runtime
  - código JS recibido es WRAPPER-EJECUTADO en función async, sin acceso a fs/network
    por convención (el prompt fuerza solo require('docx'))

NO usa vm2 ni isolated-vm porque agregan deps y superficie. El aislamiento real
viene del contenedor Docker; este executor es la última capa, defensiva.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import tempfile
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Tamaño máximo del .docx que aceptamos del sandbox (10 MB).
MAX_OUTPUT_BYTES = 10 * 1024 * 1024
# Timeout duro para Node.js
DEFAULT_TIMEOUT_S = 30
# Límite memoria heap V8 (MB)
DEFAULT_HEAP_MB = 256
# Tamaño máximo del JS que aceptamos (256 KB)
MAX_JS_BYTES = 256 * 1024


class NodeExecutionError(Exception):
    """Error en sandbox de Node (timeout, OOM, JS syntax, runtime)."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "runtime",
        stderr: str = "",
        duration_ms: int = 0,
    ):
        super().__init__(message)
        self.kind = kind                 # "timeout" | "oom" | "syntax" | "runtime" | "io"
        self.stderr = stderr
        self.duration_ms = duration_ms


@dataclass
class NodeExecutionResult:
    docx_bytes: bytes
    duration_ms: int
    stdout: str
    stderr: str
    sha256: str


# El wrapper que envuelve el JS de Claude. Claude solo debe definir una función
# async build(data) que retorna un Document de docx-js. El wrapper:
#   1. require('docx')
#   2. carga data del archivo temporal
#   3. ejecuta build(data)
#   4. Packer.toBuffer(doc) → escribe a fd 3 (stdout-binario alternativo)
_WRAPPER_JS = r"""
'use strict';

const docx = require('docx');
const fs = require('fs');

// Constructores expuestos para que el JS del modelo pueda usarlos sin re-require
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType,
  Table, TableRow, TableCell, WidthType, BorderStyle, ShadingType,
  PageOrientation, PageBreak, Header, Footer, LineRuleType,
  TabStopType, TabStopPosition, ImageRun, LevelFormat, SectionType,
  convertInchesToTwip, convertMillimetersToTwip,
} = docx;

async function main() {
  // El JS del modelo está embebido bajo este marcador
  const data = JSON.parse(fs.readFileSync(process.env.LEXAI_DATA_PATH, 'utf-8'));

  // ===== USER JS START =====
  /* __USER_JS_PLACEHOLDER__ */
  // ===== USER JS END =====

  if (typeof build !== 'function') {
    throw new Error('LEXAI_NO_BUILD_FUNCTION: el modelo no definió build(data)');
  }
  const doc = await build(data);
  if (!doc || typeof doc !== 'object') {
    throw new Error('LEXAI_NO_DOCUMENT: build() no retornó un Document');
  }
  const buffer = await Packer.toBuffer(doc);

  // Escribir el .docx al path indicado en LEXAI_OUT_PATH para evitar mezclar stdout texto y binario
  fs.writeFileSync(process.env.LEXAI_OUT_PATH, buffer);
  console.log('LEXAI_OK ' + buffer.length);
}

main().catch((err) => {
  console.error('LEXAI_ERROR ' + (err && err.message ? err.message : String(err)));
  if (err && err.stack) console.error(err.stack);
  process.exit(1);
});
"""


def _wrap_user_js(user_js: str) -> str:
    """Inserta el JS del modelo dentro del wrapper. Sin escapado especial:
    confiamos en que el JS no contiene el marcador. Para defenderse, validamos."""
    marker = "/* __USER_JS_PLACEHOLDER__ */"
    if marker in user_js:
        raise NodeExecutionError(
            "user JS contiene el marcador reservado del wrapper",
            kind="syntax",
        )
    return _WRAPPER_JS.replace(marker, user_js)


async def execute_docx_js(
    user_js: str,
    data: dict[str, Any],
    *,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    heap_mb: int = DEFAULT_HEAP_MB,
    node_path: Optional[str] = None,
) -> NodeExecutionResult:
    """Ejecuta el JS del modelo en un subproceso de Node.

    El JS debe definir una función ``async function build(data) { ... return new Document(...) }``.
    El wrapper de este executor llama build(data), packtea con Packer.toBuffer y guarda en LEXAI_OUT_PATH.

    Raises:
        NodeExecutionError con kind ∈ {"syntax","runtime","timeout","oom","io"}.
    """
    if not user_js or not user_js.strip():
        raise NodeExecutionError("user JS vacío", kind="syntax")
    if len(user_js.encode("utf-8")) > MAX_JS_BYTES:
        raise NodeExecutionError(
            f"user JS excede {MAX_JS_BYTES} bytes",
            kind="syntax",
        )

    wrapped = _wrap_user_js(user_js)

    # Trabajo en directorio temporal
    workdir = tempfile.mkdtemp(prefix="lexai-node-")
    try:
        js_path = Path(workdir) / "main.js"
        data_path = Path(workdir) / "data.json"
        out_path = Path(workdir) / "out.docx"

        js_path.write_text(wrapped, encoding="utf-8")
        data_path.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")

        env = os.environ.copy()
        env["LEXAI_DATA_PATH"] = str(data_path)
        env["LEXAI_OUT_PATH"] = str(out_path)
        # NODE_PATH ya viene del Dockerfile; permitir override para dev local
        if node_path:
            env["NODE_PATH"] = node_path
        elif "NODE_PATH" not in env:
            env["NODE_PATH"] = "/opt/docx-runtime/node_modules"

        cmd = [
            "node",
            "--no-warnings",
            f"--max-old-space-size={heap_mb}",
            str(js_path),
        ]

        t0 = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=workdir,
            )
        except FileNotFoundError as e:
            raise NodeExecutionError(
                f"node no disponible: {e}",
                kind="io",
            ) from e

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            duration_ms = int((time.perf_counter() - t0) * 1000)
            raise NodeExecutionError(
                f"timeout {timeout_s}s",
                kind="timeout",
                duration_ms=duration_ms,
            )

        duration_ms = int((time.perf_counter() - t0) * 1000)
        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")

        if proc.returncode != 0:
            # Detectar OOM vs error genérico
            oom = "JavaScript heap out of memory" in stderr or "fatal error" in stderr.lower()
            kind = "oom" if oom else "runtime"
            raise NodeExecutionError(
                f"node exit={proc.returncode}; first stderr line: "
                + (stderr.splitlines()[0] if stderr else "<empty>"),
                kind=kind,
                stderr=stderr,
                duration_ms=duration_ms,
            )

        if not out_path.exists():
            raise NodeExecutionError(
                "node terminó OK pero LEXAI_OUT_PATH no fue creado",
                kind="runtime",
                stderr=stderr,
                duration_ms=duration_ms,
            )

        docx_bytes = out_path.read_bytes()
        if len(docx_bytes) > MAX_OUTPUT_BYTES:
            raise NodeExecutionError(
                f"output .docx excede {MAX_OUTPUT_BYTES} bytes (got {len(docx_bytes)})",
                kind="runtime",
                duration_ms=duration_ms,
            )
        if len(docx_bytes) < 100:
            raise NodeExecutionError(
                f"output .docx demasiado pequeño ({len(docx_bytes)} bytes) — probablemente vacío",
                kind="runtime",
                duration_ms=duration_ms,
            )
        # docx (zip) empieza con PK\x03\x04
        if not docx_bytes.startswith(b"PK\x03\x04"):
            raise NodeExecutionError(
                "output no es un zip docx válido (magic mismatch)",
                kind="runtime",
                duration_ms=duration_ms,
            )

        sha = hashlib.sha256(docx_bytes).hexdigest()
        return NodeExecutionResult(
            docx_bytes=docx_bytes,
            duration_ms=duration_ms,
            stdout=stdout,
            stderr=stderr,
            sha256=sha,
        )
    finally:
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass


def health_check_sync() -> dict[str, Any]:
    """Verifica que node + docx-js están disponibles. Llamar al boot del backend."""
    import subprocess

    out: dict[str, Any] = {"node_ok": False, "docx_ok": False}
    try:
        r = subprocess.run(
            ["node", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            out["node_version"] = r.stdout.strip()
            out["node_ok"] = True
    except Exception as e:
        out["node_error"] = str(e)
        return out

    try:
        env = os.environ.copy()
        env.setdefault("NODE_PATH", "/opt/docx-runtime/node_modules")
        # docx@9.x bloquea require('docx/package.json') via exports map.
        # En su lugar verificamos que require('docx') exporta Document + Packer.
        # Si funciona, intentamos extraer la versión vía fs sin pasar por exports map.
        check_script = (
            "const d = require('docx');"
            "if (!d.Document || !d.Packer) { console.error('missing exports'); process.exit(1); }"
            "try {"
            "  const fs = require('fs'), path = require('path');"
            "  const p = path.join(require.resolve('docx').replace(/build[\\\\/].*/,'') ,'package.json');"
            "  const v = JSON.parse(fs.readFileSync(p,'utf-8')).version;"
            "  console.log(v);"
            "} catch(e) { console.log('unknown'); }"
        )
        r = subprocess.run(
            ["node", "-e", check_script],
            capture_output=True, text=True, timeout=5, env=env,
        )
        if r.returncode == 0:
            out["docx_version"] = r.stdout.strip() or "unknown"
            out["docx_ok"] = True
        else:
            out["docx_error"] = (r.stderr or "").strip()[:300]
    except Exception as e:
        out["docx_error"] = str(e)

    return out
