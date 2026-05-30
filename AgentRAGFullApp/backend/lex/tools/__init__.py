"""Sprint M20.02 · Tool Registry para el LeanOrchestrator (ReAct loop).

Las 18 tools del nuevo agente. Cada tool es un wrapper delgado sobre
código existente (verifier, hunters, calculadora, renderer, etc.) + persiste
audit en tool_call_audit.

Tools agrupadas por función:

CONTEXTO (5)
  - load_skill_md            → wrap lex/orchestrator/stages/skill_loader
  - load_playbook            → SELECT firm_playbook (nuevo wrapper)
  - extract_data             → wrap lex/orchestrator/stages/data_extractor
  - load_matter_context      → RPC lexai_matter_full_context (nuevo)
  - recall_memory            → SELECT agent_memory + FTS (nuevo wrapper)

VERIFICACIÓN (5)
  - verify_citation          → wrap lex/verify/verification_agent.VerificationAgent
  - search_jurisprudence     → wrap lex/hunters/* via hunters_stage
  - search_brave_gov         → wrap lex/verify/tools/fetch_web_search_official
  - fetch_mcp_official       → dispatcher 6 MCP servers Colombia (S9)
  - check_derogation         → wrap lex/verify/derogation_verifier

GENERACIÓN (5)
  - generate_clause          → wrap lex/orchestrator/stages/block_generator (atomizado)
  - check_completeness       → wrap lex/orchestrator/stages/completeness_check
  - check_coherence          → wrap lex/orchestrator/stages/coherence_check
  - validate_legal           → wrap legal_classifier + qa fusionados
  - calc_legal               → wrap lex/calc/* (+ sandbox opcional S8)

SALIDA & PERSISTENCIA (3)
  - build_docx               → wrap lex/renderer/python_docx_builder + claude_docx_renderer
  - narrate_progress         → wrap lex/blocks/events + chat_messages
  - persist_audit            → wrap lex/storage/audit_repo + blocks_repo + chat
"""

from .base import (
    ToolDef,
    ToolCall,
    ToolResult,
    ToolError,
    ToolContext,
    ToolStatus,
)
from .registry import ToolRegistry
from .dispatcher import ToolDispatcher

__all__ = [
    "ToolDef",
    "ToolCall",
    "ToolResult",
    "ToolError",
    "ToolContext",
    "ToolStatus",
    "ToolRegistry",
    "ToolDispatcher",
]
