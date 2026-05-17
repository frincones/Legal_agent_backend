"""LLM-as-Router primitive · classify input into one of N typed buckets.

Used by:
  - intent disambiguation inside the multi-agent orchestrator (Sprint 3)
  - template kind detection ("¿usuario quiere demanda o tutela?")
  - materia disambiguation when matter context is ambiguous

Tier: ROUTER (gpt-4o-mini · cheap + fast).

Design notes:
  - Uses native json_schema output (structured outputs) for zero parse errors
  - Returns a confidence score so the orchestrator can fall back to clarifying
    questions when the model is uncertain
  - Stateless · no DB dependency · safe to call from anywhere
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from utils.llm_tiers import (
    Tier,
    estimate_cost_cents,
    get_tier_config,
    llm_generate_with_schema,
)

from .base import LLMSkillResult, SkillExecutionError


@dataclass
class RouterDecision:
    """Result payload of classify_intent().

    choice: one of the option keys provided to classify_intent
    confidence: 0.0 - 1.0 self-reported by the model
    reasoning: one-sentence rationale (in Spanish · for UI display)
    alternatives: ranked next-best choices with their scores
    """
    choice: str
    confidence: float
    reasoning: str
    alternatives: list[dict]


_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["choice", "confidence", "reasoning", "alternatives"],
    "properties": {
        "choice": {"type": "string"},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
        "alternatives": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["choice", "score"],
                "properties": {
                    "choice": {"type": "string"},
                    "score": {"type": "number"},
                },
            },
        },
    },
}


async def classify_intent(
    *,
    input_text: str,
    options: dict[str, str],
    context_hint: Optional[str] = None,
    purpose: str = "router",
    session_id: str = "",
) -> LLMSkillResult:
    """Classify input_text into one of options.keys().

    Args:
        input_text: The user message / document to classify.
        options:    {key → human-readable description} for each candidate.
        context_hint: Optional extra context (e.g. "user is on case
                      Pérez vs Bavaria · materia laboral").
        purpose:    Telemetry label for usage_tracker.
        session_id: Telemetry label for usage_tracker.

    Returns:
        LLMSkillResult with data=RouterDecision.

    Raises:
        SkillExecutionError if the model output cannot be coerced.
    """
    if not options:
        raise SkillExecutionError(
            "options dict cannot be empty",
            skill="classify_intent",
        )

    started = time.time()
    cfg = get_tier_config(Tier.ROUTER)

    options_block = "\n".join(f"  - {k}: {v}" for k, v in options.items())
    sys_prompt = (
        "Eres un clasificador. Lee la entrada del usuario y elige la mejor "
        "opción de la lista. Da una confianza honesta (no infles). "
        "Si dudas entre dos opciones, refleja eso en `alternatives`."
    )
    user_prompt = (
        f"OPCIONES:\n{options_block}\n\n"
        + (f"CONTEXTO: {context_hint}\n\n" if context_hint else "")
        + f"ENTRADA:\n{input_text}\n\n"
        "Devuelve un JSON con `choice` (clave exacta de la opción elegida), "
        "`confidence` (0-1), `reasoning` (una oración en español), y "
        "`alternatives` (top-N siguientes con sus scores)."
    )

    try:
        raw = await llm_generate_with_schema(
            tier=Tier.ROUTER,
            prompt=user_prompt,
            schema=_SCHEMA,
            schema_name="router_decision",
            system_prompt=sys_prompt,
            purpose=purpose,
            session_id=session_id,
        )
    except Exception as e:
        raise SkillExecutionError(
            f"router LLM call failed: {e}",
            skill="classify_intent",
            cause=e,
        ) from e

    choice = raw.get("choice", "")
    if choice not in options:
        # Strict-mode json_schema can still hallucinate · validate enum here.
        # Fall back to the highest-scored alternative if it's valid.
        alts = raw.get("alternatives") or []
        rescued = next((a for a in alts if a.get("choice") in options), None)
        if rescued:
            choice = rescued["choice"]
            raw["reasoning"] = (raw.get("reasoning") or "") + " (rescued from alternatives)"
        else:
            raise SkillExecutionError(
                f"router returned invalid choice '{choice}' not in {list(options)}",
                skill="classify_intent",
            )

    decision = RouterDecision(
        choice=choice,
        confidence=float(raw.get("confidence", 0.0)),
        reasoning=str(raw.get("reasoning", "")),
        alternatives=list(raw.get("alternatives") or []),
    )

    duration_ms = int((time.time() - started) * 1000)
    return LLMSkillResult(
        skill="classify_intent",
        success=True,
        data=decision,
        model=cfg.model,
        # Token counts are tracked inside llm_generate_with_schema via
        # usage_tracker · we don't double-count here · cost stays 0 in
        # this envelope (use usage_tracker.stats() for the real total).
        duration_ms=duration_ms,
    )
