"""LLM tier selector — thin wrapper around utils/llm.py.

DOES NOT modify utils/llm.py. Adds a small abstraction so the new
multi-agent code (Sprint 3) and llm_skills primitives (Sprint 1) can
say `pick_model(Tier.JUDGE)` instead of hard-coding model strings.

Three tiers map to the OpenAI lineup used across LexAI:

    Tier.ROUTER   → gpt-4o-mini    · fast, cheap, used for intent/classification
    Tier.WORKER   → gpt-4o         · main workhorse · drafter, editor, slot-filler
    Tier.JUDGE    → o3-mini        · reasoning · critic, judge, validator

These are DEFAULTS · per-firm overrides can come from `firms.plan` via
the existing entitlements layer (Sprint 4 work).

Cost helpers and prompt-caching guidance live here too so they're one
import for the whole multi-agent module.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from utils.llm import (
    get_openai_client,
    llm_generate,
    llm_generate_json,
)
from utils.usage_tracker import tracker

logger = logging.getLogger(__name__)


class Tier(str, Enum):
    """Logical roles · resolved to model names by pick_model()."""
    ROUTER = "router"      # fast classification / routing
    WORKER = "worker"      # main generation / extraction
    JUDGE = "judge"        # reasoning-heavy critique / validation


# Default mapping. Override per env if a firm-specific plan needs another model.
_TIER_DEFAULTS: dict[Tier, str] = {
    Tier.ROUTER: os.getenv("LEXAI_MODEL_ROUTER", "gpt-4o-mini"),
    Tier.WORKER: os.getenv("LEXAI_MODEL_WORKER", "gpt-4o"),
    Tier.JUDGE:  os.getenv("LEXAI_MODEL_JUDGE",  "o3-mini"),
}


@dataclass(frozen=True)
class TierConfig:
    """Per-tier defaults that callers can override per call."""
    model: str
    temperature: float
    max_tokens: int
    supports_json_schema: bool
    supports_streaming: bool


_TIER_CONFIG: dict[Tier, TierConfig] = {
    Tier.ROUTER: TierConfig(
        model=_TIER_DEFAULTS[Tier.ROUTER],
        temperature=0.0,
        max_tokens=500,
        supports_json_schema=True,
        supports_streaming=True,
    ),
    Tier.WORKER: TierConfig(
        model=_TIER_DEFAULTS[Tier.WORKER],
        temperature=0.3,
        max_tokens=4000,
        supports_json_schema=True,
        supports_streaming=True,
    ),
    Tier.JUDGE: TierConfig(
        # o3-mini does NOT support response_format json_schema as of release.
        # Use json_object mode + strict prompt schema · same as llm_generate_json.
        model=_TIER_DEFAULTS[Tier.JUDGE],
        temperature=1.0,  # reasoning models require temperature=1
        max_tokens=4000,
        supports_json_schema=False,
        supports_streaming=False,
    ),
}


def pick_model(tier: Tier) -> str:
    """Return the OpenAI model name for a logical tier."""
    return _TIER_CONFIG[tier].model


def get_tier_config(tier: Tier) -> TierConfig:
    """Return the full per-tier config (model + sampling defaults)."""
    return _TIER_CONFIG[tier]


# ──────────────────────────────────────────────────────────────
# Cost estimation · keeps us honest about multi-agent budgets.
# Numbers are USD cents per 1M tokens (input | output) · update as
# OpenAI revises pricing. Last reviewed: 2026-05.
# ──────────────────────────────────────────────────────────────
_PRICING_USD_PER_MTOKEN: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o":      (2.50, 10.00),
    "o3-mini":     (1.10, 4.40),
}


def estimate_cost_cents(model: str, tokens_in: int, tokens_out: int) -> int:
    """Estimate OpenAI cost in USD CENTS for a single call."""
    if model not in _PRICING_USD_PER_MTOKEN:
        return 0
    in_per_mtok, out_per_mtok = _PRICING_USD_PER_MTOKEN[model]
    dollars = (tokens_in / 1_000_000) * in_per_mtok + (tokens_out / 1_000_000) * out_per_mtok
    return int(dollars * 100)


# ──────────────────────────────────────────────────────────────
# Convenience: tier-aware text and JSON helpers.
# These delegate to utils/llm.py's tracked helpers, so cost/usage
# accounting (usage_tracker) keeps working without changes.
# ──────────────────────────────────────────────────────────────
async def llm_generate_tier(
    tier: Tier,
    prompt: str,
    *,
    system_prompt: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    purpose: str = "",
    session_id: str = "",
) -> str:
    """Tier-aware text generation · returns plain string."""
    cfg = get_tier_config(tier)
    return await llm_generate(
        prompt=prompt,
        model=cfg.model,
        system_prompt=system_prompt,
        temperature=temperature if temperature is not None else cfg.temperature,
        max_tokens=max_tokens if max_tokens is not None else cfg.max_tokens,
        purpose=purpose,
        session_id=session_id,
    )


async def llm_generate_json_tier(
    tier: Tier,
    prompt: str,
    *,
    system_prompt: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    purpose: str = "",
    session_id: str = "",
) -> dict:
    """Tier-aware JSON-mode generation · returns parsed dict.

    Use this for any structured-output need from the multi-agent module.
    """
    cfg = get_tier_config(tier)
    return await llm_generate_json(
        prompt=prompt,
        model=cfg.model,
        system_prompt=system_prompt,
        temperature=temperature if temperature is not None else cfg.temperature,
        max_tokens=max_tokens if max_tokens is not None else cfg.max_tokens,
        purpose=purpose,
        session_id=session_id,
    )


async def llm_generate_with_schema(
    tier: Tier,
    prompt: str,
    schema: dict[str, Any],
    schema_name: str,
    *,
    system_prompt: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    purpose: str = "",
    session_id: str = "",
) -> dict:
    """Structured output via OpenAI response_format=json_schema (strict).

    Falls back to json_object mode if the tier's model doesn't support
    json_schema (e.g. o3-mini at time of release).
    """
    cfg = get_tier_config(tier)

    # o3-mini fallback · honor schema-aware prompt and parse manually.
    if not cfg.supports_json_schema:
        sys = system_prompt or ""
        sys += (
            f"\n\nResponde con un JSON válido que cumpla EXACTAMENTE este "
            f"schema (sin texto adicional, sin markdown):\n{schema}"
        )
        return await llm_generate_json_tier(
            tier,
            prompt,
            system_prompt=sys,
            temperature=temperature,
            max_tokens=max_tokens,
            purpose=purpose,
            session_id=session_id,
        )

    # Native json_schema path · gpt-4o / gpt-4o-mini.
    import json as _json
    client = get_openai_client()
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    resp = await client.chat.completions.create(
        model=cfg.model,
        messages=messages,
        temperature=temperature if temperature is not None else cfg.temperature,
        max_tokens=max_tokens if max_tokens is not None else cfg.max_tokens,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "schema": schema,
                "strict": True,
            },
        },
    )

    if resp.usage:
        tracker.record_chat(
            model=cfg.model,
            input_tokens=resp.usage.prompt_tokens,
            output_tokens=resp.usage.completion_tokens,
            purpose=purpose,
            session_id=session_id,
        )

    raw = (resp.choices[0].message.content or "").strip()
    return _json.loads(raw)
