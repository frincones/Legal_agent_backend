"""Sprint M20.03 · LeanOrchestrator · entry point del nuevo path ReAct.

Reemplaza el Orchestrator legacy de 1 603 líneas (17 stages). Instancia
ToolRegistry + AnthropicBrain y delega todo el flujo al loop ReAct.

Mantiene compatibilidad con el contrato GenerationRequest existente y emite
los mismos 30 eventos SSE.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Optional
from uuid import UUID, uuid4

from lex.brain import AnthropicBrain, BrainConfig
from lex.tools import ToolContext, ToolRegistry

logger = logging.getLogger(__name__)


class LeanOrchestrator:
    """Orquestador delgado: Tool Registry + Brain ReAct loop."""

    def __init__(
        self,
        *,
        anthropic_client,
        openai_client=None,
        pool=None,
        firm_id: Optional[UUID] = None,
        user_id: Optional[UUID] = None,
        generation_id: Optional[UUID] = None,
        mcp_clients: Optional[dict] = None,
        brain_config: Optional[BrainConfig] = None,
    ):
        self.pool = pool
        self.firm_id = firm_id
        self.user_id = user_id
        self.generation_id = generation_id or uuid4()
        self.anthropic_client = anthropic_client
        self.openai_client = openai_client

        self.registry = ToolRegistry(
            pool=pool,
            anthropic_client=anthropic_client,
            openai_client=openai_client,
            mcp_clients=mcp_clients or {},
        )
        self.brain = AnthropicBrain(
            anthropic_client=anthropic_client,
            openai_client=openai_client,
            config=brain_config or BrainConfig(),
        )

    async def run(
        self,
        *,
        intent: str,
        brief: str = "",
        doc_type_hint: str = "",
        matter_id: Optional[UUID] = None,
        borrador_mode: bool = True,
    ) -> AsyncIterator[bytes]:
        """Ejecuta el agente. Yieldea bytes SSE listos para FastAPI StreamingResponse.

        Args:
            borrador_mode: True (default) = el Brain redacta con [PLACEHOLDER]
                cuando faltan datos. False = modo firma, el Brain valida
                completeness antes de redactar.
        """
        ctx = ToolContext(
            generation_id=self.generation_id,
            firm_id=self.firm_id,
            user_id=self.user_id,
            matter_id=matter_id,
            pool=self.pool,
            anthropic_client=self.anthropic_client,
            openai_client=self.openai_client,
        )

        # Carga el playbook una vez (no lo invocamos como tool del Brain
        # porque queremos que sea parte del system prompt cacheable).
        playbook_raw_md = await self._load_playbook_md()

        async for sse_bytes in self.brain.react_loop(
            registry=self.registry,
            ctx=ctx,
            intent=intent,
            brief=brief,
            doc_type_hint=doc_type_hint,
            playbook_raw_md=playbook_raw_md,
            borrador_mode=borrador_mode,
        ):
            yield sse_bytes

    async def _load_playbook_md(self) -> Optional[str]:
        """Carga el raw_md del firm_playbook para inyectar como system prompt."""
        if self.pool is None or self.firm_id is None:
            return None
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "select raw_md from firm_playbook where firm_id = $1 limit 1",
                    self.firm_id,
                )
                if row and row["raw_md"]:
                    return row["raw_md"]
        except Exception as e:
            logger.debug("LeanOrchestrator._load_playbook_md fail: %s", e)
        return None
