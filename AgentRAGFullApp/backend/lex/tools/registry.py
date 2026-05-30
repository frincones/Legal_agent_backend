"""Sprint M20.02 · ToolRegistry · catálogo de las 18 tools.

Singleton que conoce las 18 tools del Brain. El LeanOrchestrator
hace:
    registry = ToolRegistry(pool=pool, clients={"anthropic":..., "openai":...})
    tools_schema = registry.schema_for_anthropic()
    tool = registry.get("verify_citation")
    result = await dispatcher.execute(call, registry, ctx)
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .base import ToolDef

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Registro de tools. Inicializado con dependencias (pool, clients)."""

    def __init__(
        self,
        pool: Any = None,
        anthropic_client: Any = None,
        openai_client: Any = None,
        mcp_clients: Optional[dict] = None,
        extra: Optional[dict] = None,
    ):
        self.pool = pool
        self.anthropic_client = anthropic_client
        self.openai_client = openai_client
        self.mcp_clients = mcp_clients or {}
        self.extra = extra or {}
        self._tools: dict[str, ToolDef] = {}
        self._auto_register()

    def _auto_register(self) -> None:
        """Importa e instancia las 18 tools del paquete lex.tools.* .

        Cada wrapper define su propia clase ToolDef y la instanciamos aquí
        con el pool + clients necesarios.
        """
        # imports diferidos para evitar circulares
        from . import (
            load_skill_md,
            load_playbook,
            extract_data,
            load_matter_context,
            recall_memory,
            verify_citation,
            search_jurisprudence,
            search_brave_gov,
            fetch_mcp_official,
            check_derogation,
            generate_clause,
            check_completeness,
            check_coherence,
            validate_legal,
            calc_legal,
            build_docx,
            narrate_progress,
            persist_audit,
        )
        modules = [
            load_skill_md, load_playbook, extract_data, load_matter_context, recall_memory,
            verify_citation, search_jurisprudence, search_brave_gov, fetch_mcp_official, check_derogation,
            generate_clause, check_completeness, check_coherence, validate_legal, calc_legal,
            build_docx, narrate_progress, persist_audit,
        ]
        for mod in modules:
            tool = mod.build_tool(
                pool=self.pool,
                anthropic_client=self.anthropic_client,
                openai_client=self.openai_client,
                mcp_clients=self.mcp_clients,
                extra=self.extra,
            )
            self.register(tool)

    def register(self, tool: ToolDef) -> None:
        if tool.name in self._tools:
            logger.warning("ToolRegistry: tool %r ya registrada, sobrescribiendo", tool.name)
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[ToolDef]:
        return self._tools.get(name)

    def list(self) -> list[ToolDef]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def schema_for_anthropic(self) -> list[dict]:
        """Lista de schemas en formato Anthropic tool_use."""
        return [t.schema_dict() for t in self._tools.values()]

    def __len__(self) -> int:
        return len(self._tools)

    def __repr__(self) -> str:
        return f"<ToolRegistry tools={len(self)}: {', '.join(sorted(self._tools.keys()))}>"
