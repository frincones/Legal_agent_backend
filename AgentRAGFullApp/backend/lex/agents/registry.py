"""Sprint M21.S4.E · AgentRegistry singleton."""
from __future__ import annotations

import logging
from typing import Any, Optional

from .base import BackgroundAgent

logger = logging.getLogger(__name__)


class AgentRegistry:
    def __init__(self):
        self._agents: dict[str, BackgroundAgent] = {}
        self._auto_register()

    def _auto_register(self) -> None:
        from . import (
            learn_from_seed_docs,
            extract_practice_patterns,
            derogation_sweeper,
            matter_summary_refresher,
        )
        for mod in [learn_from_seed_docs, extract_practice_patterns, derogation_sweeper, matter_summary_refresher]:
            agent = mod.build_agent()
            self.register(agent)

    def register(self, agent: BackgroundAgent) -> None:
        if agent.name in self._agents:
            logger.warning("AgentRegistry: %r overwriting", agent.name)
        self._agents[agent.name] = agent

    def get(self, name: str) -> Optional[BackgroundAgent]:
        return self._agents.get(name)

    def list(self) -> list[BackgroundAgent]:
        return list(self._agents.values())

    def names(self) -> list[str]:
        return sorted(self._agents.keys())

    def __len__(self) -> int:
        return len(self._agents)


# Singleton
_registry: Optional[AgentRegistry] = None


def get_agent_registry() -> AgentRegistry:
    global _registry
    if _registry is None:
        _registry = AgentRegistry()
    return _registry
