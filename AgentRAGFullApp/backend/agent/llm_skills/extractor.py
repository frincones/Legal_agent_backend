"""LLM-as-Extractor primitive · extract structured fields from free text.

Used by:
  - agent/tools/slot_filler.py (Sprint 3) · pulls parties/dates/amounts
    out of conversation + document_extractions for template slot filling
  - intent extraction at the start of multi-agent generation

Tier: ROUTER (gpt-4o-mini) for short text, WORKER (gpt-4o) for long docs.
Caller picks via the `tier` arg · default ROUTER.

Design notes:
  - Caller declares fields with name, description, type, required, examples
  - We synthesize a JSON schema and call llm_generate_with_schema
  - Missing fields come back as null (not omitted) so callers can detect
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

from utils.llm_tiers import (
    Tier,
    get_tier_config,
    llm_generate_with_schema,
)

from .base import LLMSkillResult, SkillExecutionError


@dataclass
class ExtractionField:
    """Describes a single field the extractor should look for."""
    name: str
    description: str
    type: str = "string"          # JSON schema type · string|number|boolean|array
    required: bool = False
    examples: Optional[list[Any]] = None
    enum: Optional[list[str]] = None


def _build_schema(fields: list[ExtractionField]) -> dict[str, Any]:
    """Convert ExtractionField list into a JSON Schema object."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for f in fields:
        prop: dict[str, Any] = {"description": f.description}
        # Allow null so missing fields don't fail strict mode validation.
        if f.enum:
            prop["enum"] = list(f.enum) + [None] if not f.required else list(f.enum)
            prop["type"] = ["string", "null"] if not f.required else "string"
        elif f.type == "array":
            prop["type"] = ["array", "null"] if not f.required else "array"
            prop["items"] = {"type": "string"}
        else:
            prop["type"] = [f.type, "null"] if not f.required else f.type
        if f.examples:
            prop["examples"] = f.examples
        properties[f.name] = prop
        if f.required:
            required.append(f.name)
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required or [f.name for f in fields],  # strict needs all
    }


async def extract_structured(
    *,
    text: str,
    fields: list[ExtractionField],
    instructions: Optional[str] = None,
    tier: Tier = Tier.ROUTER,
    purpose: str = "extractor",
    session_id: str = "",
) -> LLMSkillResult:
    """Extract a set of named fields from free text.

    Args:
        text: Source text to parse.
        fields: Fields to extract · each with name + description.
        instructions: Optional extra rules (e.g. "format dates as YYYY-MM-DD").
        tier: Defaults to ROUTER · upgrade to WORKER for long/complex docs.

    Returns:
        LLMSkillResult with data=dict[field.name → value | None].

    Raises:
        SkillExecutionError on LLM failure.
    """
    if not fields:
        raise SkillExecutionError(
            "fields list cannot be empty",
            skill="extract_structured",
        )

    started = time.time()
    cfg = get_tier_config(tier)
    schema = _build_schema(fields)

    sys_prompt = (
        "Eres un extractor de información jurídica colombiana. Lee el texto "
        "y devuelve EXACTAMENTE los campos solicitados. Si un campo no aparece "
        "explícitamente o no se puede inferir con confianza, ponlo en null · "
        "nunca inventes datos."
    )
    if instructions:
        sys_prompt += "\n\nReglas adicionales: " + instructions

    user_prompt = "TEXTO FUENTE:\n" + text.strip()

    try:
        data = await llm_generate_with_schema(
            tier=tier,
            prompt=user_prompt,
            schema=schema,
            schema_name="extracted_fields",
            system_prompt=sys_prompt,
            purpose=purpose,
            session_id=session_id,
        )
    except Exception as e:
        raise SkillExecutionError(
            f"extractor LLM call failed: {e}",
            skill="extract_structured",
            cause=e,
        ) from e

    # Normalize · ensure every declared field is present (null if missing)
    for f in fields:
        data.setdefault(f.name, None)

    duration_ms = int((time.time() - started) * 1000)
    return LLMSkillResult(
        skill="extract_structured",
        success=True,
        data=data,
        model=cfg.model,
        duration_ms=duration_ms,
    )
