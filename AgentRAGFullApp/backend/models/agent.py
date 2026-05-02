"""Data models for agent configuration and state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AgentConfig:
    """Configuration for the agent."""

    name: str = "RAG Agent"
    role: str = "Knowledge Assistant"
    primary_model: str = "gpt-4o"
    utility_model: str = "gpt-4o-mini"
    temperature: float = 0.3
    max_tokens: int = 2000
    system_prompt_template: Optional[str] = None

    # Custom domain tools
    custom_tools_module: Optional[str] = None

    # Database tables for structured queries
    db_tables_schema: Optional[str] = None


@dataclass
class AgentState:
    """Runtime state of the agent during a conversation."""

    conversation_history: List[Dict[str, str]] = field(default_factory=list)
    last_retrieval_context: Optional[str] = None
    last_intent: Optional[str] = None
    total_queries: int = 0
    total_tokens_used: int = 0

    def add_message(self, role: str, content: str) -> None:
        self.conversation_history.append({"role": role, "content": content})

    def get_history(self, max_messages: int = 20) -> List[Dict[str, str]]:
        return self.conversation_history[-max_messages:]

    def clear(self) -> None:
        self.conversation_history.clear()
        self.last_retrieval_context = None
        self.last_intent = None
