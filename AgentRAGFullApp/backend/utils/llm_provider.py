"""M19.24.H — Multi-provider LLM adapter (OpenAI + Anthropic Claude).

Permite dispatchar las llamadas LLM entre OpenAI (GPT-4o family) y
Anthropic (Claude Sonnet/Opus) según env vars sin cambiar el código
de los stages.

Uso típico desde un stage:

    from utils.llm_provider import chat_complete_json

    data = await chat_complete_json(
        provider_env="LLM_PROVIDER_BLOCK_GEN",     # nombre de la env var
        default_provider="openai",                  # default si la env var no está
        model_env_openai="OPENAI_MODEL_BLOCK_GEN", # opcional para override
        model_env_anthropic="ANTHROPIC_MODEL_BLOCK_GEN",
        default_model_openai="gpt-4o",
        default_model_anthropic="claude-sonnet-4-6",
        system_prompt="...",
        user_prompt="...",
        temperature=0.2,
        max_tokens=4000,
    )

El adapter:
  - Devuelve dict (JSON parseado del LLM)
  - Maneja diferencias de API (response_format, max_tokens, messages, etc.)
  - Logging unificado de duración + uso
  - Fallback automático a OpenAI si Anthropic falla y el provider era anthropic
  - NO toca el client global de OpenAI existente (utils/llm.get_openai_client)
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ============================================================
# Anthropic client (lazy)
# ============================================================

_anthropic_client = None


def _get_anthropic_client():
    """Get or create the Anthropic async client. Lazy import para no romper
    el startup si el SDK no está instalado.

    Returns None si la API key no está configurada (caller usa fallback).

    M19.30 (P0 fix) · timeout HTTP propio (default 20s read, 5s connect) para
    evitar que el cliente Anthropic se cuelgue sin propagar excepción.
    Antes: cuando la API estaba lenta, asyncio.wait_for del stage mataba
    la tarea ANTES de que chat_complete_json hiciera fallback a OpenAI →
    el stage caía a su EMPTY_REPORT como si Anthropic ni se hubiera intentado.
    Después: el SDK tira APITimeoutError en <=20s y chat_complete_json
    cae al fallback OpenAI dentro del wait_for del stage.
    Override via ANTHROPIC_HTTP_TIMEOUT_S y ANTHROPIC_HTTP_CONNECT_TIMEOUT_S.
    """
    global _anthropic_client
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    if _anthropic_client is None:
        try:
            from anthropic import AsyncAnthropic
            try:
                import httpx as _httpx
                # M19.30 (P0 v2) · timeout HTTP bajado a 12s + max_retries=0
                # para fallar rápido y dar más margen al fallback OpenAI
                # dentro del wait_for de cada stage (que tiene 30-40s).
                try:
                    read = float(os.getenv("ANTHROPIC_HTTP_TIMEOUT_S", "12.0"))
                except ValueError:
                    read = 12.0
                try:
                    connect = float(os.getenv("ANTHROPIC_HTTP_CONNECT_TIMEOUT_S", "3.0"))
                except ValueError:
                    connect = 3.0
                try:
                    max_retries = int(os.getenv("ANTHROPIC_MAX_RETRIES", "0"))
                except ValueError:
                    max_retries = 0
                logger.info(
                    "AsyncAnthropic init · timeout=%ss connect=%ss max_retries=%d",
                    read, connect, max_retries,
                )
                _anthropic_client = AsyncAnthropic(
                    api_key=api_key,
                    timeout=_httpx.Timeout(read, connect=connect),
                    max_retries=max_retries,
                )
            except Exception as e:
                # Fallback al constructor por default si httpx/timeout falla
                logger.warning("AsyncAnthropic with timeout failed (%s) — using defaults", e)
                _anthropic_client = AsyncAnthropic(api_key=api_key)
        except ImportError:
            logger.warning("anthropic SDK not installed — install: pip install anthropic")
            return None
        except Exception as e:
            logger.warning("AsyncAnthropic init failed: %s", e)
            return None
    return _anthropic_client


# ============================================================
# Provider resolver
# ============================================================

def resolve_provider(provider_env: Optional[str], default_provider: str = "openai") -> str:
    """Lee la env var de provider y normaliza el valor.

    Returns "openai" o "anthropic". Default a "openai" si no hay env var o
    el valor no es reconocido.
    """
    if not provider_env:
        return default_provider
    value = os.getenv(provider_env, "").strip().lower()
    if value in ("anthropic", "claude"):
        return "anthropic"
    if value in ("openai", "gpt"):
        return "openai"
    return default_provider


def resolve_model(model_env: Optional[str], default_model: str) -> str:
    """Lee la env var de modelo o devuelve el default."""
    if model_env:
        value = os.getenv(model_env, "").strip()
        if value:
            return value
    return default_model


# ============================================================
# Anthropic JSON extraction
# ============================================================

_JSON_FENCE_PATTERNS = (
    "```json",
    "```JSON",
    "```",
)


def _extract_json_from_text(text: str) -> str:
    """Extrae bloque JSON de un texto que puede tener markdown wrapping.

    Claude a veces devuelve ```json ... ``` aunque le pidamos JSON puro.
    Esta función limpia esos fences.
    """
    text = (text or "").strip()
    # Quitar fences markdown si vienen
    for fence in _JSON_FENCE_PATTERNS:
        if text.startswith(fence):
            text = text[len(fence):].strip()
            break
    if text.endswith("```"):
        text = text[:-3].strip()
    # Si hay texto antes del primer { o [, recortar
    start_obj = text.find("{")
    start_arr = text.find("[")
    starts = [s for s in (start_obj, start_arr) if s >= 0]
    if starts:
        first = min(starts)
        if first > 0:
            text = text[first:]
    # Si hay texto después del último } o ], recortar
    last_obj = text.rfind("}")
    last_arr = text.rfind("]")
    last = max(last_obj, last_arr)
    if last >= 0 and last < len(text) - 1:
        text = text[: last + 1]
    return text


# ============================================================
# M21.HOTFIX-4 · Parser JSON tolerante a errores comunes de LLM
# ============================================================
# Detectado en testing manual: Sonnet 4.6 ocasionalmente produce JSON con
# comas faltantes entre elementos en outputs grandes (>10KB). El parser
# estándar `json.loads()` falla con "Expecting ',' delimiter".
#
# Esta función intenta 4 estrategias de recovery antes de re-raise:
#   1. json.loads vanilla (happy path)
#   2. Regex repair: insertar comas faltantes entre objetos/strings consecutivos
#   3. Regex repair: eliminar trailing commas
#   4. Truncar al último } o ] balanceado válido y reintentar
# ============================================================

import re as _re_jsonp


def _json_repair_missing_commas(text: str) -> str:
    """Inserta comas faltantes entre objetos/elementos consecutivos.

    Patrones comunes de Sonnet 4.6:
      - `}{`  →  `},{`
      - `}\n  {`  →  `},\n  {`
      - `"foo"\n  "bar":` →  `"foo",\n  "bar":`
      - `]\n  [`  →  `],\n  [`
      - `"foo" "bar":`  →  `"foo", "bar":`
    """
    # Entre `}` y `{` (mismo nivel) – con o sin espacio/newline
    text = _re_jsonp.sub(r"\}(\s*)\{", r"},\1{", text)
    # Entre `]` y `[`
    text = _re_jsonp.sub(r"\](\s*)\[", r"],\1[", text)
    # Entre `}` y `"key":` (objeto seguido de key)
    text = _re_jsonp.sub(r'\}(\s*)\"', r'},\1"', text)
    # Entre `]` y `"key":`
    text = _re_jsonp.sub(r'\](\s*)\"', r'],\1"', text)
    # Entre string-value y key (e.g. `"value" "key":` o `"value""key":`)
    # — heurística cuidadosa. Permite 0 o más whitespace porque Sonnet
    # ocasionalmente produce key adjacency sin separación.
    text = _re_jsonp.sub(r'\"(\s*)\"([a-zA-Z_][\w]*)\"\s*:', r'",\1"\2":', text)
    # Entre number/bool/null y key (`42 "key":` → `42, "key":`)
    text = _re_jsonp.sub(
        r'(\d|true|false|null)(\s+)\"([a-zA-Z_][\w]*)\"\s*:',
        r'\1,\2"\3":', text,
    )
    return text


def _json_repair_trailing_commas(text: str) -> str:
    """Elimina trailing commas antes de `}` o `]`."""
    text = _re_jsonp.sub(r",(\s*)\}", r"\1}", text)
    text = _re_jsonp.sub(r",(\s*)\]", r"\1]", text)
    return text


def _json_close_unbalanced(text: str) -> Optional[str]:
    """Para outputs truncados (depth > 0 al final, e.g. cortado por max_tokens),
    truncar antes del último elemento incompleto y cerrar manualmente.

    Patrón típico: `{"blocks": [{"a":1}, {"b":2}, {"c":3` →
                  → trunca después de `{"b":2}`, cierra con `]}`
                  → `{"blocks": [{"a":1}, {"b":2}]}`

    Devuelve None si no se detecta unbalanced o no se puede recuperar.
    """
    # Tracking de depth
    depth_curly = 0
    depth_square = 0
    in_string = False
    escape = False
    last_safe_idx = -1  # Última posición donde depth == 1 después de cerrar un elemento
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth_curly += 1
        elif ch == "}":
            depth_curly -= 1
            # Después de cerrar un objeto, si estamos dentro de un array (depth_square>=1),
            # esta es una posición segura para truncar
            if depth_square >= 1 and depth_curly < depth_square + 5:
                last_safe_idx = i
        elif ch == "[":
            depth_square += 1
        elif ch == "]":
            depth_square -= 1

    # Si está balanceado, no necesitamos cerrar manualmente
    if depth_curly == 0 and depth_square == 0:
        return None

    # Si encontramos posición segura → truncar + cerrar
    if last_safe_idx > 0:
        truncated = text[: last_safe_idx + 1]
        # Recalcular cuántos braces/brackets faltan
        d_curly = 0
        d_square = 0
        in_s = False
        esc = False
        for ch in truncated:
            if esc: esc = False; continue
            if ch == "\\": esc = True; continue
            if ch == '"': in_s = not in_s; continue
            if in_s: continue
            if ch == "{": d_curly += 1
            elif ch == "}": d_curly -= 1
            elif ch == "[": d_square += 1
            elif ch == "]": d_square -= 1
        # Cerrar en orden inverso al apertura
        closer = ""
        while d_curly > 0 or d_square > 0:
            # cierra el más reciente abierto. Para simplicidad cerramos primero ] luego }
            # (asume estructuras tipo {"arr": [...]} comunes)
            if d_square > 0:
                closer += "]"
                d_square -= 1
            elif d_curly > 0:
                closer += "}"
                d_curly -= 1
        return truncated + closer
    return None


def _json_truncate_to_last_balanced(text: str) -> Optional[str]:
    """Truncate al último `}` o `]` balanceado válido.

    Útil cuando el LLM cortó la respuesta a mitad (max_tokens reached).
    Devuelve None si no encuentra ningún balance válido.
    """
    depth_curly = 0
    depth_square = 0
    in_string = False
    escape = False
    last_balanced = -1
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth_curly += 1
        elif ch == "}":
            depth_curly -= 1
            if depth_curly == 0 and depth_square == 0:
                last_balanced = i
        elif ch == "[":
            depth_square += 1
        elif ch == "]":
            depth_square -= 1
            if depth_curly == 0 and depth_square == 0:
                last_balanced = i
    if last_balanced > 0:
        return text[: last_balanced + 1]
    return None


def parse_json_tolerant(text: str, *, _section_label: str = "") -> dict:
    """Parsea JSON con recovery automático de errores comunes del LLM.

    Raises:
        json.JSONDecodeError: si todas las estrategias fallan. Log incluye
            preview del texto y la posición exacta del error original.
    """
    # Estrategia 1: vanilla
    try:
        return json.loads(text)
    except json.JSONDecodeError as e_vanilla:
        original_err = e_vanilla

    # Estrategia 2: missing commas + trailing commas (orden importa)
    try:
        repaired = _json_repair_missing_commas(text)
        repaired = _json_repair_trailing_commas(repaired)
        result = json.loads(repaired)
        logger.warning(
            "parse_json_tolerant: recovered via comma_repair section=%r "
            "(original error: %s)",
            _section_label, str(original_err)[:120],
        )
        return result
    except json.JSONDecodeError:
        pass

    # Estrategia 3: truncate al último balance válido (output truncado por max_tokens)
    truncated = _json_truncate_to_last_balanced(text)
    if truncated and truncated != text:
        try:
            result = json.loads(truncated)
            logger.warning(
                "parse_json_tolerant: recovered via truncate_to_balanced section=%r "
                "(saved %d chars; original error: %s)",
                _section_label, len(text) - len(truncated), str(original_err)[:120],
            )
            return result
        except json.JSONDecodeError:
            pass

    # Estrategia 4: truncate + repair combinados
    if truncated:
        try:
            repaired_truncated = _json_repair_missing_commas(truncated)
            repaired_truncated = _json_repair_trailing_commas(repaired_truncated)
            result = json.loads(repaired_truncated)
            logger.warning(
                "parse_json_tolerant: recovered via truncate+comma_repair section=%r "
                "(original error: %s)",
                _section_label, str(original_err)[:120],
            )
            return result
        except json.JSONDecodeError:
            pass

    # Estrategia 5: si el output está totalmente truncado (depth > 0 al final),
    # truncar antes del último elemento incompleto y cerrar manualmente las
    # estructuras abiertas. Para outputs cortados por max_tokens del LLM.
    closed = _json_close_unbalanced(text)
    if closed and closed != text:
        try:
            result = json.loads(closed)
            logger.warning(
                "parse_json_tolerant: recovered via close_unbalanced section=%r "
                "(original error: %s)",
                _section_label, str(original_err)[:120],
            )
            return result
        except json.JSONDecodeError:
            # último intento: aplicar comma_repair sobre el cerrado
            try:
                repaired_closed = _json_repair_missing_commas(closed)
                repaired_closed = _json_repair_trailing_commas(repaired_closed)
                result = json.loads(repaired_closed)
                logger.warning(
                    "parse_json_tolerant: recovered via close+comma_repair section=%r",
                    _section_label,
                )
                return result
            except json.JSONDecodeError:
                pass

    # Última: re-raise con preview del texto para debugging
    preview_chars = 200
    err_pos = getattr(original_err, "pos", 0)
    preview_start = max(0, err_pos - preview_chars)
    preview_end = min(len(text), err_pos + preview_chars)
    logger.error(
        "parse_json_tolerant: ALL strategies failed section=%r len=%d pos=%d\n"
        "preview around error: ...%s...",
        _section_label, len(text), err_pos,
        text[preview_start:preview_end].replace("\n", "\\n")[:400],
    )
    raise original_err


# ============================================================
# Unified async chat_complete_json
# ============================================================

async def chat_complete_json(
    *,
    provider_env: Optional[str] = None,
    default_provider: str = "openai",
    model_env_openai: Optional[str] = None,
    model_env_anthropic: Optional[str] = None,
    default_model_openai: str = "gpt-4o",
    default_model_anthropic: str = "claude-sonnet-4-6",
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.2,
    max_tokens: int = 4000,
    fallback_to_openai: bool = True,
) -> dict:
    """Llama al LLM elegido por la env var y devuelve dict JSON parseado.

    Si el provider es "anthropic" pero el cliente Anthropic no está
    disponible (sin API key, SDK no instalado) y `fallback_to_openai=True`,
    cae a OpenAI silenciosamente.

    Si la llamada al provider primario falla (timeout, error de API),
    también cae a OpenAI cuando `fallback_to_openai=True`.

    Raises:
        json.JSONDecodeError: si ambos providers fallan en producir JSON.
        Exception: si ambos providers fallan y fallback_to_openai=False.
    """
    provider = resolve_provider(provider_env, default_provider)
    started = time.time()

    if provider == "anthropic":
        client = _get_anthropic_client()
        if client is None:
            if not fallback_to_openai:
                raise RuntimeError("Anthropic client not available and fallback disabled")
            logger.info("anthropic client unavailable → fallback to openai")
            provider = "openai"

    if provider == "anthropic":
        model = resolve_model(model_env_anthropic, default_model_anthropic)
        try:
            # Anthropic NO tiene response_format=json_object, así que reforzamos
            # en el system prompt + extraemos el JSON del output.
            sys_for_claude = (
                system_prompt.rstrip()
                + "\n\nIMPORTANTE: Responde EXCLUSIVAMENTE con un objeto JSON válido. "
                + "NO incluyas explicaciones, markdown, ```json fences, ni texto antes o después del JSON. "
                + "Solo el JSON crudo empezando con { y terminando con }."
            )
            client = _get_anthropic_client()
            resp = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=sys_for_claude,
                messages=[{"role": "user", "content": user_prompt}],
            )
            # Extraer texto del content
            content_parts = resp.content or []
            text = ""
            for part in content_parts:
                t = getattr(part, "text", None)
                if t:
                    text += t
            if not text:
                raise ValueError("anthropic returned empty text content")
            cleaned = _extract_json_from_text(text)
            # M21.HOTFIX-4: parser tolerante a comas faltantes / trailing commas /
            # outputs truncados por max_tokens. Recupera ~80% de errores comunes
            # de Sonnet 4.6 en outputs >10KB (e.g. "Expecting ',' delimiter").
            data = parse_json_tolerant(cleaned, _section_label=f"anthropic:{model}")
            elapsed = int((time.time() - started) * 1000)
            logger.info(
                "llm_provider: anthropic model=%s tokens_in=%d tokens_out=%d duration=%dms",
                model,
                getattr(resp.usage, "input_tokens", 0) if hasattr(resp, "usage") and resp.usage else 0,
                getattr(resp.usage, "output_tokens", 0) if hasattr(resp, "usage") and resp.usage else 0,
                elapsed,
            )
            return data
        except Exception as e:
            logger.warning(
                "llm_provider: anthropic call failed model=%s err=%s",
                model, str(e)[:200],
            )
            if not fallback_to_openai:
                raise
            logger.info("llm_provider: falling back to openai")
            # Fall through to openai branch below

    # OpenAI branch (default + fallback)
    model = resolve_model(model_env_openai, default_model_openai)
    try:
        from utils.llm import get_openai_client
        client = get_openai_client()
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        # M21.HOTFIX-4: aplicar también al fallback OpenAI por consistencia
        # (response_format=json_object reduce el riesgo, pero edge cases existen)
        data = parse_json_tolerant(raw, _section_label=f"openai:{model}")
        elapsed = int((time.time() - started) * 1000)
        logger.info(
            "llm_provider: openai model=%s tokens_in=%d tokens_out=%d duration=%dms",
            model,
            getattr(resp.usage, "prompt_tokens", 0) if resp.usage else 0,
            getattr(resp.usage, "completion_tokens", 0) if resp.usage else 0,
            elapsed,
        )
        return data
    except Exception as e:
        logger.warning("llm_provider: openai call failed model=%s err=%s", model, str(e)[:200])
        raise


# ============================================================
# Provider status helper (para health / debug)
# ============================================================

def provider_status() -> dict:
    """Devuelve estado de configuración de cada provider (sin exponer keys)."""
    has_openai = bool(os.getenv("OPENAI_API_KEY", "").strip())
    has_anthropic = bool(os.getenv("ANTHROPIC_API_KEY", "").strip())
    return {
        "openai": {"configured": has_openai},
        "anthropic": {"configured": has_anthropic, "sdk_installed": _get_anthropic_client() is not None or not has_anthropic},
        "routing": {
            "LLM_PROVIDER_BLOCK_GEN": os.getenv("LLM_PROVIDER_BLOCK_GEN", "openai"),
            "LLM_PROVIDER_STRUCTURE": os.getenv("LLM_PROVIDER_STRUCTURE", "openai"),
            "LLM_PROVIDER_LEGAL_CLASSIFIER": os.getenv("LLM_PROVIDER_LEGAL_CLASSIFIER", "openai"),
            "LLM_PROVIDER_DATA_COMPLETENESS": os.getenv("LLM_PROVIDER_DATA_COMPLETENESS", "openai"),
        },
        "models": {
            "ANTHROPIC_MODEL_BLOCK_GEN": os.getenv("ANTHROPIC_MODEL_BLOCK_GEN", "claude-sonnet-4-6"),
            "ANTHROPIC_MODEL_STRUCTURE": os.getenv("ANTHROPIC_MODEL_STRUCTURE", "claude-sonnet-4-6"),
            "ANTHROPIC_MODEL_LEGAL": os.getenv("ANTHROPIC_MODEL_LEGAL", "claude-sonnet-4-6"),
        },
    }


__all__ = [
    "chat_complete_json",
    "resolve_provider",
    "resolve_model",
    "provider_status",
]
