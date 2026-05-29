"""LLM client factory for primary and utility models with usage tracking."""

from __future__ import annotations

import logging
import os
from typing import List, Optional

import httpx
from openai import AsyncOpenAI

from utils.usage_tracker import tracker

logger = logging.getLogger(__name__)


_client: Optional[AsyncOpenAI] = None


# M19.30 — Fix bloqueo del chat: el SDK de OpenAI por default usa timeout
# de 600 s. Cuando gpt-4o degrada, las llamadas se cuelgan hasta que el
# wrapper asyncio.wait_for de cada stage corta (20-40 s) y el usuario ve
# el spinner mudo. Fijamos timeout=15s para fallar rápido y caer al
# fallback (Anthropic/legacy) sin esperar al timeout del stage. Overridable
# vía OPENAI_HTTP_TIMEOUT_S y OPENAI_HTTP_CONNECT_TIMEOUT_S.
def _openai_timeout() -> httpx.Timeout:
    # M19.30 (P0 fix v2) · default bajado de 15s → 10s. El SDK OpenAI hace
    # retries internos con backoff: si max_retries=1 y timeout=15s, una
    # llamada lenta puede costar ~30s antes de fallar → no le da margen al
    # fallback dentro del wait_for del stage. Bajar a 10s + max_retries=0
    # garantiza fallar en <12s.
    try:
        read = float(os.getenv("OPENAI_HTTP_TIMEOUT_S", "10.0"))
    except ValueError:
        read = 10.0
    try:
        connect = float(os.getenv("OPENAI_HTTP_CONNECT_TIMEOUT_S", "3.0"))
    except ValueError:
        connect = 3.0
    return httpx.Timeout(read, connect=connect)


def get_openai_client() -> AsyncOpenAI:
    """Get or create a shared AsyncOpenAI client.

    Configura timeout HTTP propio para que si gpt-4o no responde, falle
    en segundos en lugar de quedarse colgado el default de 600s del SDK.
    """
    global _client
    if _client is None:
        timeout = _openai_timeout()
        try:
            # M19.30 (P0 v2) · max_retries=0 por default. Con APIs degradadas
            # cada retry agrega 5-10s más. Mejor fallar rápido.
            max_retries = int(os.getenv("OPENAI_MAX_RETRIES", "0"))
        except ValueError:
            max_retries = 0
        logger.info(
            "AsyncOpenAI init · timeout=%ss connect=%ss max_retries=%d",
            timeout.read, timeout.connect, max_retries,
        )
        _client = AsyncOpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            timeout=timeout,
            max_retries=max_retries,
        )
    return _client


async def llm_generate(
    prompt: str,
    model: str = "gpt-4o-mini",
    system_prompt: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: int = 500,
    purpose: str = "",
    session_id: str = "",
) -> str:
    """Simple LLM text generation helper with usage tracking."""
    client = get_openai_client()
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    response = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )

    # Record usage
    if response.usage:
        tracker.record_chat(
            model=model,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            purpose=purpose,
            session_id=session_id,
        )

    return response.choices[0].message.content.strip()


async def llm_generate_json(
    prompt: str,
    model: str = "gpt-4o-mini",
    system_prompt: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: int = 4000,
    purpose: str = "",
    session_id: str = "",
) -> dict:
    """LLM text generation with JSON-mode response. Returns parsed dict.

    System prompt MUST instruct the model to output a JSON object
    (per OpenAI requirements for response_format json_object).
    """
    import json as _json
    client = get_openai_client()
    messages = []
    sys = (system_prompt or "") + (
        "\n\nResponde EXCLUSIVAMENTE con un JSON válido siguiendo el esquema solicitado. "
        "Sin texto explicativo. Sin markdown."
    )
    messages.append({"role": "system", "content": sys.strip()})
    messages.append({"role": "user", "content": prompt})

    response = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )

    if response.usage:
        tracker.record_chat(
            model=model,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            purpose=purpose,
            session_id=session_id,
        )

    raw = response.choices[0].message.content.strip()
    return _json.loads(raw)


async def llm_generate_embedding(
    text: str,
    model: str = "text-embedding-3-small",
    purpose: str = "",
    session_id: str = "",
) -> List[float]:
    """Generate a single embedding vector with usage tracking."""
    client = get_openai_client()
    response = await client.embeddings.create(
        model=model,
        input=text,
    )

    if response.usage:
        tracker.record_embedding(
            model=model,
            input_tokens=response.usage.prompt_tokens,
            purpose=purpose,
            session_id=session_id,
        )

    return response.data[0].embedding


async def llm_generate_embeddings_batch(
    texts: List[str],
    model: str = "text-embedding-3-small",
    purpose: str = "",
    session_id: str = "",
) -> List[List[float]]:
    """Generate embedding vectors for a batch of texts with usage tracking."""
    client = get_openai_client()
    response = await client.embeddings.create(
        model=model,
        input=texts,
    )

    if response.usage:
        tracker.record_embedding(
            model=model,
            input_tokens=response.usage.prompt_tokens,
            purpose=purpose,
            session_id=session_id,
        )

    return [item.embedding for item in response.data]
