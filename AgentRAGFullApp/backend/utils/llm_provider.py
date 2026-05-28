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
    """
    global _anthropic_client
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    if _anthropic_client is None:
        try:
            from anthropic import AsyncAnthropic
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
            data = json.loads(cleaned)
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
        data = json.loads(raw)
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
