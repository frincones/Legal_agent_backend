"""LLM Skills base · shared types and error class.

Kept deliberately small: each primitive has its own Result dataclass.
This base only holds the shape that all primitives return so callers
(the multi-agent orchestrator) can aggregate uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


class SkillExecutionError(Exception):
    """Raised when a primitive cannot produce a usable result."""

    def __init__(self, message: str, *, skill: str, cause: Optional[Exception] = None):
        super().__init__(message)
        self.skill = skill
        self.cause = cause


@dataclass
class LLMSkillResult:
    """Generic envelope all primitives return.

    The orchestrator uses this for telemetry. The actual payload is in
    `data` and its shape is documented per primitive.
    """
    skill: str
    success: bool
    data: Any
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_cents: int = 0
    duration_ms: int = 0
    error: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
