"""Claude .docx renderer (camino B) · pipeline LLM → JS → docx.

Flujo:
  1. Cargar SKILL.md del template (firm_skills, builtin o custom)
  2. Cargar built-in docx skill (lex/renderer/skills/docx/SKILL.md)
  3. Construir prompt para Claude con: docx skill + template + datos del usuario
  4. Llamar Claude (Opus 4.7 por default) → recibe JS
  5. Ejecutar JS en sandbox node_executor → recibe bytes .docx
  6. Si falla: retry hasta MAX_RETRIES con stderr en el prompt
  7. Si excede retries: fallback a lex/docx_forensic_builder.py
  8. Auditar en claude_render_audit (success/error/fallback)

Feature flags (env):
  - CLAUDE_RENDERER_ENABLED          (default false)
  - CLAUDE_RENDERER_DOC_FAMILIES     (CSV: notarial,contractual,...)
  - CLAUDE_RENDERER_FIRM_IDS         (CSV de UUIDs)
  - CLAUDE_RENDERER_MODEL            (default claude-opus-4-7)
  - CLAUDE_RENDERER_MAX_RETRIES      (default 2)
  - CLAUDE_RENDERER_TIMEOUT_S        (default 30)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .node_executor import (
    DEFAULT_TIMEOUT_S,
    NodeExecutionError,
    execute_docx_js,
)

logger = logging.getLogger(__name__)

# Path to the built-in docx skill (relative to this file)
_BUILTIN_SKILL_DIR = Path(__file__).parent / "skills" / "docx"
_BUILTIN_SKILL_FILE = _BUILTIN_SKILL_DIR / "SKILL.md"

# M19.30 (29 may 2026) · Default model bajado de Opus 4.7 → Sonnet 4.6.
# Opus 4.7 estaba intermitente en producción (timeouts a 12s + bug de
# parámetro `temperature` deprecado). Sonnet 4.6 responde más rápido y
# consistente con calidad suficiente para generar el .docx del SKILL.md.
# Revertir a Opus via env CLAUDE_RENDERER_MODEL=claude-opus-4-7 si se
# estabiliza la API.
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_RETRIES = 2
DEFAULT_MAX_TOKENS = 8192


# ============================================================
# Public API
# ============================================================


@dataclass
class RenderRequest:
    """Datos para una invocación del renderer Claude."""

    doc_type: str
    template_skill_md: str          # Contenido SKILL.md del template (notarial_poder_especial_co, etc.)
    user_prompt: str                # Instrucción del usuario ("redactar poder para X")
    data: dict[str, Any]            # Placeholders del usuario
    firm_id: Optional[str] = None
    user_id: Optional[str] = None
    matter_id: Optional[str] = None
    document_id: Optional[str] = None
    skill_id: Optional[str] = None  # firm_skills.id

    # Configuración (None = lee de env)
    model: Optional[str] = None
    max_retries: Optional[int] = None
    timeout_s: Optional[int] = None
    temperature: float = 0.2


@dataclass
class RenderResult:
    docx_bytes: bytes
    sha256: str
    generated_js: str
    js_sha256: str
    duration_ms: int
    llm_duration_ms: int
    sandbox_duration_ms: int
    retry_count: int
    tokens_input: int
    tokens_output: int
    cost_usd_cents: int
    model: str
    audit_id: Optional[str] = None  # claude_render_audit.id

    @property
    def status(self) -> str:
        return "success"


# ============================================================
# Feature flag evaluation
# ============================================================


def is_renderer_enabled_for(
    *,
    doc_type: Optional[str] = None,
    doc_family: Optional[str] = None,
    firm_id: Optional[str] = None,
) -> bool:
    """Evalúa si el renderer Claude debe usarse para este caso.

    Reglas:
      1. CLAUDE_RENDERER_ENABLED debe estar true
      2. Si CLAUDE_RENDERER_DOC_FAMILIES está seteado: doc_family debe estar incluido
      3. Si CLAUDE_RENDERER_FIRM_IDS está seteado: firm_id debe estar incluido
    """
    if os.getenv("CLAUDE_RENDERER_ENABLED", "false").strip().lower() not in ("1", "true", "yes", "on"):
        return False

    families_env = os.getenv("CLAUDE_RENDERER_DOC_FAMILIES", "").strip()
    if families_env:
        allowed = {f.strip().lower() for f in families_env.split(",") if f.strip()}
        if doc_family and doc_family.strip().lower() not in allowed:
            return False

    firms_env = os.getenv("CLAUDE_RENDERER_FIRM_IDS", "").strip()
    if firms_env and firm_id:
        allowed_firms = {f.strip() for f in firms_env.split(",") if f.strip()}
        if firm_id.strip() not in allowed_firms:
            return False

    return True


# ============================================================
# Prompt construction
# ============================================================


def _load_builtin_skill() -> str:
    """Carga el SKILL.md built-in docx (cached por process)."""
    if not _BUILTIN_SKILL_FILE.exists():
        raise FileNotFoundError(
            f"Built-in docx skill missing: {_BUILTIN_SKILL_FILE}. "
            "Verifica que lex/renderer/skills/docx/SKILL.md fue empaquetado."
        )
    return _BUILTIN_SKILL_FILE.read_text(encoding="utf-8")


_BUILTIN_SKILL_CACHE: Optional[str] = None


def get_builtin_skill_md() -> str:
    global _BUILTIN_SKILL_CACHE
    if _BUILTIN_SKILL_CACHE is None:
        _BUILTIN_SKILL_CACHE = _load_builtin_skill()
    return _BUILTIN_SKILL_CACHE


def _build_system_prompt(template_skill_md: str) -> str:
    """Construye el system prompt para Claude.

    Estructura:
      1. Identidad + tarea
      2. Built-in docx skill (cómo usar docx-js)
      3. Template SKILL.md del doc_type (estructura legal específica)
      4. Reglas de seguridad
    """
    builtin = get_builtin_skill_md()
    return (
        "Eres LexAI Renderer: un asistente especializado en convertir prompts del "
        "usuario y plantillas legales colombianas en código JavaScript ejecutable "
        "que produce archivos .docx profesionales mediante la librería docx-js@9.5 "
        "(Node.js).\n\n"
        "Tu salida DEBE ser EXACTAMENTE un único bloque ```javascript ... ``` que "
        "define `async function build(data) { ... return new Document({...}); }` "
        "y nada más. Sin comentarios explicativos fuera del bloque. Sin texto "
        "antes o después.\n\n"
        "============================================================\n"
        "PARTE A · BUILT-IN DOCX SKILL (cómo usar docx-js)\n"
        "============================================================\n"
        f"{builtin}\n\n"
        "============================================================\n"
        "PARTE B · TEMPLATE DEL DOC_TYPE (qué redactar y con qué estructura)\n"
        "============================================================\n"
        f"{template_skill_md}\n\n"
        "============================================================\n"
        "REGLAS FINALES\n"
        "============================================================\n"
        "1. Tu output es UN solo bloque ```javascript ... ``` con la función build.\n"
        "2. NO uses require, fs, http, child_process, eval, Function, process.env.\n"
        "3. Los constructores de docx-js están en scope global (NO los re-requieres).\n"
        "4. Sustituye placeholders [XYZ] con data.xyz (o el campo equivalente). Si "
        "   data no tiene el campo, deja el placeholder literal con corchetes.\n"
        "5. Todo el contenido debe ser en español formal jurídico colombiano.\n"
        "6. NO inventes citas normativas: usa exactamente las que aparecen en el template.\n"
        "7. Cuando el template diga 'Risk Warnings', NO incluyas esas advertencias en\n"
        "   el .docx (son para tu validación interna).\n"
        "8. Sigue la sección 'Document Structure' del template EN ORDEN ESTRICTO.\n"
        "   Si el template prescribe encabezados con ordinales 'PRIMERA',\n"
        "   'SEGUNDA', 'TERCERA', ... úsalos LITERALMENTE — NO los reemplaces por\n"
        "   numeración romana 'I.', 'II.', 'III.' ni por palabras como 'PODERDANTE'\n"
        "   o 'FACULTADES OTORGADAS' (esos son estilos de demanda judicial, no de\n"
        "   poder notarial). Cada documento tiene su propio estilo: respétalo.\n"
        "9. Si en `data` recibes texto crudo (`data.raw_text` o `data.sections`),\n"
        "   considera ese contenido SOLO como fuente de datos del usuario (nombres,\n"
        "   fechas, números). NO copies su estructura ni sus encabezados — la\n"
        "   estructura final viene EXCLUSIVAMENTE del template SKILL.md.\n"
    )


def _build_user_message(req: RenderRequest, *, retry_context: Optional[str] = None) -> str:
    """Construye el mensaje user para Claude."""
    parts = [
        f"DOC_TYPE: {req.doc_type}\n",
        f"INSTRUCCIÓN DEL USUARIO:\n{req.user_prompt}\n\n",
        "DATOS DISPONIBLES (objeto `data` que recibirás como parámetro):\n",
        "```json\n",
        json.dumps(req.data, ensure_ascii=False, indent=2, default=str),
        "\n```\n",
    ]
    if retry_context:
        parts.append(
            "\n\n============================================================\n"
            "INTENTO PREVIO FALLÓ — ERRORES PARA QUE CORRIJAS\n"
            "============================================================\n"
            f"{retry_context}\n\n"
            "Genera de nuevo el bloque ```javascript build(data) corregido. "
            "Si el error fue 'syntax', revisa paréntesis/llaves. Si fue 'runtime', "
            "revisa que todos los constructores existan en docx@9.5 y no haya null."
        )
    return "".join(parts)


def _extract_js_block(text: str) -> str:
    """Extrae el primer bloque ```javascript ... ``` del output de Claude."""
    if not text:
        return ""
    s = text.strip()
    # Fence variantes
    for opener in ("```javascript", "```js", "```JavaScript", "```"):
        idx = s.find(opener)
        if idx >= 0:
            after = s[idx + len(opener):].lstrip("\n")
            end = after.find("```")
            if end >= 0:
                return after[:end].strip()
            # No closing → tomar todo lo restante (best-effort)
            return after.strip()
    # No fence → asumir que todo es código (modelo no obedeció instrucción de fence)
    return s.strip()


# ============================================================
# Main pipeline
# ============================================================


async def render_docx(req: RenderRequest) -> RenderResult:
    """Genera un .docx con Claude + docx-js.

    Raises:
        NodeExecutionError   si el sandbox falla después de los retries
        RuntimeError         si Claude API no disponible o config inválida
    """
    from utils.llm_provider import _get_anthropic_client  # type: ignore

    model = req.model or os.getenv("CLAUDE_RENDERER_MODEL", "").strip() or DEFAULT_MODEL
    max_retries = req.max_retries if req.max_retries is not None else int(
        os.getenv("CLAUDE_RENDERER_MAX_RETRIES", str(DEFAULT_MAX_RETRIES)) or DEFAULT_MAX_RETRIES
    )
    timeout_s = req.timeout_s or int(
        os.getenv("CLAUDE_RENDERER_TIMEOUT_S", str(DEFAULT_TIMEOUT_S)) or DEFAULT_TIMEOUT_S
    )

    client = _get_anthropic_client()
    if client is None:
        raise RuntimeError(
            "Anthropic client unavailable. Set ANTHROPIC_API_KEY and install `anthropic`."
        )

    system_prompt = _build_system_prompt(req.template_skill_md)

    total_t0 = time.perf_counter()
    total_llm_ms = 0
    total_sandbox_ms = 0
    total_tokens_in = 0
    total_tokens_out = 0
    last_js = ""
    last_error: Optional[NodeExecutionError] = None
    retry_context: Optional[str] = None

    # Some Claude models (Opus 4.7+) deprecated the `temperature` param.
    # Build kwargs once; drop temperature on demand if the API rejects it.
    _send_temperature = True

    for attempt in range(max_retries + 1):
        user_msg = _build_user_message(req, retry_context=retry_context)

        # 1. Llamada a Claude
        llm_t0 = time.perf_counter()
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=DEFAULT_MAX_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        if _send_temperature:
            kwargs["temperature"] = req.temperature
        try:
            response = await client.messages.create(**kwargs)
        except Exception as e:
            msg = str(e)
            # Opus 4.7+: "temperature is deprecated for this model" → retry sin temperature
            if _send_temperature and "temperature" in msg and (
                "deprecated" in msg.lower() or "not supported" in msg.lower()
            ):
                _send_temperature = False
                logger.info("Model %s rejected temperature; retrying without it", model)
                kwargs.pop("temperature", None)
                try:
                    response = await client.messages.create(**kwargs)
                except Exception as e2:
                    raise RuntimeError(f"Claude API error (sin temperature): {e2}") from e2
            else:
                raise RuntimeError(f"Claude API error: {e}") from e
        llm_ms = int((time.perf_counter() - llm_t0) * 1000)
        total_llm_ms += llm_ms

        # Sumar tokens
        usage = getattr(response, "usage", None)
        if usage:
            total_tokens_in += int(getattr(usage, "input_tokens", 0) or 0)
            total_tokens_out += int(getattr(usage, "output_tokens", 0) or 0)

        # 2. Extraer JS
        content_parts = getattr(response, "content", []) or []
        text = ""
        for part in content_parts:
            ptype = getattr(part, "type", None)
            if ptype == "text":
                text += getattr(part, "text", "") or ""
        user_js = _extract_js_block(text)
        if not user_js:
            retry_context = (
                f"El intento {attempt + 1} no incluyó un bloque ```javascript en la respuesta. "
                "Asegúrate de que tu respuesta sea EXACTAMENTE un bloque "
                "```javascript ... ``` con la función build(data)."
            )
            last_js = ""
            continue
        last_js = user_js

        # 3. Ejecutar en sandbox
        try:
            exec_result = await execute_docx_js(
                user_js=user_js,
                data=req.data,
                timeout_s=timeout_s,
            )
            total_sandbox_ms += exec_result.duration_ms
            total_ms = int((time.perf_counter() - total_t0) * 1000)
            cost_cents = _estimate_cost_usd_cents(
                model=model,
                tokens_in=total_tokens_in,
                tokens_out=total_tokens_out,
            )
            return RenderResult(
                docx_bytes=exec_result.docx_bytes,
                sha256=exec_result.sha256,
                generated_js=user_js,
                js_sha256=hashlib.sha256(user_js.encode("utf-8")).hexdigest(),
                duration_ms=total_ms,
                llm_duration_ms=total_llm_ms,
                sandbox_duration_ms=total_sandbox_ms,
                retry_count=attempt,
                tokens_input=total_tokens_in,
                tokens_output=total_tokens_out,
                cost_usd_cents=cost_cents,
                model=model,
            )
        except NodeExecutionError as e:
            total_sandbox_ms += e.duration_ms
            last_error = e
            stderr_excerpt = (e.stderr or "").strip().splitlines()
            stderr_excerpt = "\n".join(stderr_excerpt[:20])  # primeras 20 líneas
            retry_context = (
                f"Intento {attempt + 1} falló con kind={e.kind!r}: {str(e)}\n"
                f"Primeras líneas de stderr:\n{stderr_excerpt}\n\n"
                "Corrige el JS y vuelve a generar."
            )
            logger.info(
                "claude_docx_renderer retry %d/%d (kind=%s)",
                attempt + 1, max_retries, e.kind,
            )

    # Si llegamos aquí, agotamos retries
    assert last_error is not None
    raise last_error


def _estimate_cost_usd_cents(*, model: str, tokens_in: int, tokens_out: int) -> int:
    """Estimación grosera de costo en centavos USD. Pricing 2026-05 Anthropic.

    Sonnet 4.6: $3 / 1M input, $15 / 1M output
    Opus 4.7:   $15 / 1M input, $75 / 1M output
    Haiku 4.5:  $1 / 1M input, $5 / 1M output

    Returns int (cents). No es exacto; solo para tracking/alertas.
    """
    m = (model or "").lower()
    if "opus" in m:
        usd = (tokens_in / 1_000_000) * 15.0 + (tokens_out / 1_000_000) * 75.0
    elif "haiku" in m:
        usd = (tokens_in / 1_000_000) * 1.0 + (tokens_out / 1_000_000) * 5.0
    else:
        # default sonnet
        usd = (tokens_in / 1_000_000) * 3.0 + (tokens_out / 1_000_000) * 15.0
    return int(round(usd * 100))


# ============================================================
# Audit logging (best-effort)
# ============================================================


async def log_audit(
    pool,
    *,
    req: RenderRequest,
    result: Optional[RenderResult] = None,
    error: Optional[BaseException] = None,
    fallback_used: bool = False,
    fallback_reason: Optional[str] = None,
) -> Optional[str]:
    """Inserta una fila en claude_render_audit. Best-effort: si falla, log y sigue."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                insert into claude_render_audit(
                    firm_id, user_id, matter_id, document_id, skill_id,
                    doc_type, prompt_summary, prompt_tokens, user_payload_summary,
                    llm_model, llm_temperature, retry_count,
                    generated_js, generated_js_sha256, generated_js_bytes,
                    output_docx_bytes, output_docx_sha256,
                    status, error_message, fallback_used, fallback_reason,
                    duration_ms, llm_duration_ms, sandbox_duration_ms,
                    tokens_input, tokens_output, cost_usd_cents,
                    completed_at
                ) values (
                    $1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::uuid,
                    $6, $7, $8, $9::jsonb,
                    $10, $11, $12,
                    $13, $14, $15,
                    $16, $17,
                    $18, $19, $20, $21,
                    $22, $23, $24,
                    $25, $26, $27,
                    now()
                ) returning id
                """,
                req.firm_id, req.user_id, req.matter_id, req.document_id, req.skill_id,
                req.doc_type,
                (req.user_prompt or "")[:2000],
                None,
                json.dumps({k: ("<pii>" if isinstance(v, str) and len(v) > 200 else v) for k, v in (req.data or {}).items()}, default=str),
                (result.model if result else (req.model or DEFAULT_MODEL)),
                float(req.temperature),
                (result.retry_count if result else 0),
                (result.generated_js if result else None),
                (result.js_sha256 if result else None),
                (len(result.generated_js.encode("utf-8")) if result and result.generated_js else None),
                (len(result.docx_bytes) if result else None),
                (result.sha256 if result else None),
                ("success" if result and not error else ("fallback_legacy" if fallback_used else _classify_error(error))),
                (str(error) if error else None),
                fallback_used,
                fallback_reason,
                (result.duration_ms if result else None),
                (result.llm_duration_ms if result else None),
                (result.sandbox_duration_ms if result else None),
                (result.tokens_input if result else None),
                (result.tokens_output if result else None),
                (result.cost_usd_cents if result else None),
            )
            return str(row["id"]) if row else None
    except Exception as e:
        logger.warning("claude_render_audit insert failed: %s", e)
        return None


def _classify_error(error: Optional[BaseException]) -> str:
    if error is None:
        return "running"
    if isinstance(error, NodeExecutionError):
        m = {"syntax": "js_error", "runtime": "js_error", "timeout": "sandbox_timeout", "oom": "sandbox_oom", "io": "js_error"}
        return m.get(error.kind, "js_error")
    return "llm_error"
