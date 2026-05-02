"""LLM client factory for primary and utility models with usage tracking."""

from __future__ import annotations

import os
from typing import List, Optional

from openai import AsyncOpenAI

from utils.usage_tracker import tracker


_client: Optional[AsyncOpenAI] = None


def get_openai_client() -> AsyncOpenAI:
    """Get or create a shared AsyncOpenAI client."""
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
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
