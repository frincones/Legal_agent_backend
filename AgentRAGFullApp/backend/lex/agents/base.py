"""Sprint M21.S4 · BackgroundAgent base class.

Cada agent:
  - name: str (unique, snake_case)
  - description: str
  - trigger_kind: 'cron' | 'event'
  - default_cron: cron expr si es 'cron'
  - async run(ctx) -> AgentRunResult

Dispatch + audit (agent_run_logs) lo maneja `lex/agents/dispatcher.py`.
"""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Optional
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)


@dataclass
class AgentContext:
    """Contexto inyectado al agent en cada ejecucion."""
    run_id: UUID
    firm_id: UUID
    job_id: Optional[UUID] = None
    trigger_kind: Literal["cron", "event", "manual"] = "cron"
    pool: Any = None
    anthropic_client: Any = None
    openai_client: Any = None
    config: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


@dataclass
class AgentRunResult:
    """Resultado normalizado de cualquier agent."""
    status: Literal["ok", "error", "skipped", "timeout"]
    items_processed: int = 0
    items_succeeded: int = 0
    items_failed: int = 0
    output_summary: str = ""
    error_message: Optional[str] = None
    cost_usd: float = 0.0
    metadata: dict = field(default_factory=dict)


class BackgroundAgent(abc.ABC):
    name: str = "base"
    description: str = ""
    trigger_kind: Literal["cron", "event"] = "cron"
    default_cron: Optional[str] = None
    default_event: Optional[str] = None
    timeout_seconds: float = 300.0

    @abc.abstractmethod
    async def run(self, ctx: AgentContext) -> AgentRunResult:
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<Agent name={self.name!r} trigger={self.trigger_kind}>"
