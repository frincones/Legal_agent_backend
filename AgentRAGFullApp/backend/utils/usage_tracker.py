"""Token usage and cost tracker for OpenAI API calls.

Records every chat completion and embedding call so we can report
exact costs per session, per operation, and globally.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# OpenAI pricing as of April 2026 (USD per 1M tokens)
PRICING: Dict[str, Dict[str, float]] = {
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-2024-08-06": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o-mini-2024-07-18": {"input": 0.15, "output": 0.60},
    "gpt-4-turbo": {"input": 10.00, "output": 30.00},
    "gpt-4": {"input": 30.00, "output": 60.00},
    "text-embedding-3-small": {"input": 0.02, "output": 0.0},
    "text-embedding-3-large": {"input": 0.13, "output": 0.0},
    "text-embedding-ada-002": {"input": 0.10, "output": 0.0},
}


def cost_for(model: str, input_tokens: int, output_tokens: int = 0) -> float:
    """Calculate cost in USD for a given model and token counts."""
    pricing = PRICING.get(model)
    if not pricing:
        # Best-effort match by prefix
        for key, val in PRICING.items():
            if model.startswith(key):
                pricing = val
                break
    if not pricing:
        return 0.0
    return (
        (input_tokens / 1_000_000) * pricing["input"]
        + (output_tokens / 1_000_000) * pricing["output"]
    )


@dataclass
class UsageEvent:
    """A single LLM call event."""
    timestamp: float
    model: str
    operation: str  # 'chat' | 'embedding' | 'stream'
    input_tokens: int
    output_tokens: int
    cost_usd: float
    purpose: str = ""  # e.g. 'intent_router', 'query_expansion', 'main_response'
    session_id: str = ""


@dataclass
class UsageStats:
    """Aggregated usage statistics."""
    total_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    by_model: Dict[str, Dict[str, int]] = field(default_factory=lambda: defaultdict(lambda: {"calls": 0, "input": 0, "output": 0, "cost": 0.0}))
    by_purpose: Dict[str, Dict[str, int]] = field(default_factory=lambda: defaultdict(lambda: {"calls": 0, "input": 0, "output": 0, "cost": 0.0}))
    by_session: Dict[str, Dict[str, int]] = field(default_factory=lambda: defaultdict(lambda: {"calls": 0, "input": 0, "output": 0, "cost": 0.0}))


class UsageTracker:
    """Thread-safe usage tracker."""

    def __init__(self):
        self._lock = threading.Lock()
        self._events: List[UsageEvent] = []
        self._max_events = 10000  # Cap memory usage

    def record_chat(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        purpose: str = "",
        session_id: str = "",
    ) -> UsageEvent:
        cost = cost_for(model, input_tokens, output_tokens)
        event = UsageEvent(
            timestamp=time.time(),
            model=model,
            operation="chat",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            purpose=purpose,
            session_id=session_id,
        )
        with self._lock:
            self._events.append(event)
            if len(self._events) > self._max_events:
                self._events = self._events[-self._max_events:]
        return event

    def record_embedding(
        self,
        model: str,
        input_tokens: int,
        purpose: str = "",
        session_id: str = "",
    ) -> UsageEvent:
        cost = cost_for(model, input_tokens, 0)
        event = UsageEvent(
            timestamp=time.time(),
            model=model,
            operation="embedding",
            input_tokens=input_tokens,
            output_tokens=0,
            cost_usd=cost,
            purpose=purpose,
            session_id=session_id,
        )
        with self._lock:
            self._events.append(event)
            if len(self._events) > self._max_events:
                self._events = self._events[-self._max_events:]
        return event

    def stats(self, session_id: Optional[str] = None) -> UsageStats:
        """Get aggregated stats, optionally filtered by session."""
        stats = UsageStats()
        with self._lock:
            events = self._events[:]

        for e in events:
            if session_id is not None and e.session_id != session_id:
                continue

            stats.total_calls += 1
            stats.total_input_tokens += e.input_tokens
            stats.total_output_tokens += e.output_tokens
            stats.total_cost_usd += e.cost_usd

            stats.by_model[e.model]["calls"] += 1
            stats.by_model[e.model]["input"] += e.input_tokens
            stats.by_model[e.model]["output"] += e.output_tokens
            stats.by_model[e.model]["cost"] += e.cost_usd

            if e.purpose:
                stats.by_purpose[e.purpose]["calls"] += 1
                stats.by_purpose[e.purpose]["input"] += e.input_tokens
                stats.by_purpose[e.purpose]["output"] += e.output_tokens
                stats.by_purpose[e.purpose]["cost"] += e.cost_usd

            if e.session_id:
                stats.by_session[e.session_id]["calls"] += 1
                stats.by_session[e.session_id]["input"] += e.input_tokens
                stats.by_session[e.session_id]["output"] += e.output_tokens
                stats.by_session[e.session_id]["cost"] += e.cost_usd

        return stats

    def reset(self) -> int:
        """Clear all recorded events. Returns count cleared."""
        with self._lock:
            count = len(self._events)
            self._events.clear()
        return count


# Global singleton
tracker = UsageTracker()
