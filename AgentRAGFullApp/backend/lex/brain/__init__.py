"""Sprint M20.03 · lex.brain · AnthropicBrain con ReAct loop nativo.

El Brain orquesta el LeanOrchestrator usando:
  - tool_use protocol nativo de Anthropic
  - prompt caching (cache_control ephemeral)
  - parallel tool calls (asyncio.gather)
  - fallback a OpenAI gpt-4o si Anthropic cae
  - Sonnet 4.6 default, Opus 4.7 para doc_types complejos
"""

from .anthropic_brain import AnthropicBrain, BrainConfig, OPUS_DOC_TYPES
from .sse_emitter import map_tool_to_sse_events
from .system_prompt import build_system_prompt

__all__ = [
    "AnthropicBrain",
    "BrainConfig",
    "OPUS_DOC_TYPES",
    "map_tool_to_sse_events",
    "build_system_prompt",
]
