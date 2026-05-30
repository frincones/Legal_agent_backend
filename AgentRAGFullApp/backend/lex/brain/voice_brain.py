"""Sprint M20.03 · VoiceBrain · adaptador del Brain ReAct al contexto voz.

El voice agent OpenAI Realtime expone un subset reducido de tools (las
relevantes para conversación corta y comandos legales habituales).

Reusa la misma maquinaria del AnthropicBrain pero con una whitelist de tools
y una configuración optimizada para latencia baja (max_iterations bajo,
max_tokens más estricto, sin parallel calls para evitar interrupciones del
audio bidireccional).

NOTA: la integración E2E con el WebSocket de OpenAI Realtime requiere
adaptar el handler en `api/voice.py` para que reciba mensajes texto/audio,
invoque `VoiceBrain.run_text(...)` y reenvíe la respuesta al TTS. Ese
trabajo se hace en un sprint posterior (S2.5.2) — esta capa expone la API
del lado backend lista para conectar.
"""
from __future__ import annotations

import logging
from typing import AsyncIterator, Optional
from uuid import UUID, uuid4

from lex.tools import ToolContext, ToolRegistry

from .anthropic_brain import AnthropicBrain, BrainConfig

logger = logging.getLogger(__name__)


# Subset whitelist de tools que el voice agent puede invocar.
# Excluimos: build_docx, persist_audit (no aplican en sesión voz corta),
# generate_clause (genera documentos completos, no apto voz).
VOICE_TOOLS_WHITELIST = {
    "load_skill_md",
    "load_playbook",
    "load_matter_context",
    "recall_memory",
    "verify_citation",
    "search_jurisprudence",
    "search_brave_gov",
    "check_derogation",
    "calc_legal",
    "validate_legal",
    "narrate_progress",
}


VOICE_BRAIN_CONFIG = BrainConfig(
    max_iterations=8,        # voz corta: pocas iteraciones
    max_tokens=2048,         # respuestas concisas
    max_parallel_tools=3,    # menos paralelismo para no romper audio stream
    enable_prompt_caching=True,
    enable_parallel_tools=True,
)


VOICE_SYSTEM_PROMPT_ADDENDUM = """
=== MODO VOZ ===
Estás respondiendo por voz (OpenAI Realtime). Reglas adicionales:

1. Respuestas CORTAS (1-3 frases). Sin listas largas ni párrafos extensos.
2. NO uses formato markdown, bullets, headers ni links — solo texto plano.
3. Para preguntas legales: responde con el dato + 1 cita verificada (si aplica).
4. Si necesitas más datos del usuario, pregunta UNA cosa a la vez.
5. NO generes documentos completos por voz. Si el usuario pide un documento,
   responde "Voy a generarlo en el canvas, ¿continúo?" y deja que el frontend
   trigger el flujo de documents/v2/generate.
6. Tools disponibles: verificación, búsqueda jurisprudencial, cálculos legales,
   recall de memoria. NO tienes build_docx ni generate_clause en este modo.
"""


class VoiceBrain:
    """Brain optimizado para sesión voice (OpenAI Realtime)."""

    def __init__(
        self,
        anthropic_client,
        openai_client=None,
        pool=None,
        firm_id: Optional[UUID] = None,
        user_id: Optional[UUID] = None,
        session_id: Optional[UUID] = None,
    ):
        self.pool = pool
        self.firm_id = firm_id
        self.user_id = user_id
        self.session_id = session_id or uuid4()
        self.anthropic_client = anthropic_client
        self.openai_client = openai_client

        # Registry con whitelist filtrada
        full_registry = ToolRegistry(
            pool=pool,
            anthropic_client=anthropic_client,
            openai_client=openai_client,
        )
        self.registry = _filter_registry(full_registry, VOICE_TOOLS_WHITELIST)

        self.brain = AnthropicBrain(
            anthropic_client=anthropic_client,
            openai_client=openai_client,
            config=VOICE_BRAIN_CONFIG,
        )

    async def run_text(
        self,
        *,
        user_text: str,
        matter_id: Optional[UUID] = None,
    ) -> AsyncIterator[bytes]:
        """Procesa una entrada de texto (post-STT) y yieldea SSE para el bridge voz."""
        ctx = ToolContext(
            generation_id=self.session_id,   # voice usa session_id como generation_id
            firm_id=self.firm_id,
            user_id=self.user_id,
            matter_id=matter_id,
            pool=self.pool,
            anthropic_client=self.anthropic_client,
            openai_client=self.openai_client,
        )

        from .system_prompt import build_system_prompt
        # Inyectar el addendum de voz al final del system prompt
        full_system = build_system_prompt() + VOICE_SYSTEM_PROMPT_ADDENDUM
        # Workaround: el AnthropicBrain.react_loop construye su propio system_prompt
        # desde el playbook_raw_md. Aprovechamos ese parámetro para inyectar el addendum.
        async for sse_bytes in self.brain.react_loop(
            registry=self.registry,
            ctx=ctx,
            intent=user_text,
            brief="",
            doc_type_hint="",
            playbook_raw_md=VOICE_SYSTEM_PROMPT_ADDENDUM,
        ):
            yield sse_bytes


def _filter_registry(registry: ToolRegistry, whitelist: set[str]) -> ToolRegistry:
    """Retorna un ToolRegistry nuevo que solo contiene tools en whitelist.

    En lugar de duplicar el constructor, modificamos in-place el dict interno
    (es una operación segura: registry es propio de cada brain).
    """
    filtered = {name: tool for name, tool in registry._tools.items() if name in whitelist}
    registry._tools = filtered
    return registry
