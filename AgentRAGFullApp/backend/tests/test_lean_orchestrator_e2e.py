"""Sprint M20.03 · Tests E2E del LeanOrchestrator (mocked).

Estos tests NO golpean Anthropic ni Supabase reales: usan un FakeAnthropic
que devuelve secuencias predefinidas de tool_use → end_turn. El objetivo es
validar:

  - ReAct loop converge en N iteraciones
  - tools se ejecutan en paralelo
  - SSE events del contrato se emiten correctamente
  - prompt caching headers se enviarían (verificable en el mock)
  - Sonnet vs Opus selector funciona según doc_type
  - max_iterations protege contra loops infinitos

Los tests E2E reales contra prod requieren ANTHROPIC_API_KEY + Supabase live
y se ejecutan en S3 (paridad A/B vs legacy).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest

from lex.brain import AnthropicBrain, BrainConfig, OPUS_DOC_TYPES
from lex.orchestrator.lean_orchestrator import LeanOrchestrator
from lex.tools import ToolContext, ToolRegistry


# ---- Fakes ----

@dataclass
class FakeBlock:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)


@dataclass
class FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 50
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class FakeResponse:
    stop_reason: str
    content: list = field(default_factory=list)
    usage: FakeUsage = field(default_factory=FakeUsage)


class FakeAnthropicMessages:
    """Mock del cliente Anthropic. Devuelve respuestas pre-programadas."""

    def __init__(self, scripted_responses: list[FakeResponse]):
        self.scripted = list(scripted_responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.scripted:
            # Fallback: end_turn vacío para evitar bucle infinito
            return FakeResponse(stop_reason="end_turn",
                                 content=[FakeBlock(type="text", text="(default end)")])
        return self.scripted.pop(0)


class FakeAnthropic:
    def __init__(self, scripted_responses: list[FakeResponse]):
        self.messages = FakeAnthropicMessages(scripted_responses)


@pytest.fixture
def simple_end_turn_response():
    """Respuesta simple: el Brain decide responder directamente sin tool_use."""
    return [FakeResponse(
        stop_reason="end_turn",
        content=[FakeBlock(type="text", text="Hola, soy LexAI. ¿En qué puedo ayudarte?")],
    )]


@pytest.fixture
def tool_use_then_end_response():
    """Respuesta: 1ra iter pide narrate_progress, 2da iter end_turn."""
    return [
        FakeResponse(
            stop_reason="tool_use",
            content=[
                FakeBlock(type="tool_use", id="tu-1", name="narrate_progress",
                           input={"message": "Cargando...", "kind": "narration"}),
            ],
        ),
        FakeResponse(
            stop_reason="end_turn",
            content=[FakeBlock(type="text", text="Listo. Aquí está tu respuesta.")],
        ),
    ]


# ---- Tests ----

class TestLeanOrchestratorBasics:
    @pytest.mark.asyncio
    async def test_orchestrator_instantiation(self):
        fake = FakeAnthropic([])
        orch = LeanOrchestrator(
            anthropic_client=fake,
            openai_client=None,
            pool=None,
            firm_id=uuid4(),
            user_id=uuid4(),
        )
        assert orch.registry is not None
        assert len(orch.registry) == 18
        assert orch.brain is not None

    @pytest.mark.asyncio
    async def test_simple_end_turn_emits_meta_and_done(self, simple_end_turn_response):
        fake = FakeAnthropic(simple_end_turn_response)
        orch = LeanOrchestrator(
            anthropic_client=fake, pool=None,
            firm_id=uuid4(), user_id=uuid4(),
        )
        events_received: list[tuple[str, dict]] = []
        async for sse_bytes in orch.run(intent="Hola, qué eres?"):
            text = sse_bytes.decode("utf-8")
            if text.startswith("event:"):
                lines = text.strip().split("\n")
                ev_name = lines[0].split(":", 1)[1].strip()
                payload = {}
                for line in lines[1:]:
                    if line.startswith("data:"):
                        try:
                            payload = json.loads(line[5:].strip())
                        except Exception:
                            payload = {}
                events_received.append((ev_name, payload))

        event_names = [e for e, _ in events_received]
        assert "meta" in event_names, f"expected meta event, got {event_names}"
        assert "done" in event_names, f"expected done event, got {event_names}"
        # Anthropic se llamó al menos 1 vez
        assert len(fake.messages.calls) >= 1


class TestReActLoopWithTools:
    @pytest.mark.asyncio
    async def test_tool_use_then_end_turn(self, tool_use_then_end_response):
        fake = FakeAnthropic(tool_use_then_end_response)
        orch = LeanOrchestrator(
            anthropic_client=fake, pool=None,
            firm_id=uuid4(), user_id=uuid4(),
        )
        events_received: list[str] = []
        async for sse_bytes in orch.run(intent="Saluda"):
            text = sse_bytes.decode("utf-8")
            if text.startswith("event:"):
                ev = text.split("\n")[0].split(":", 1)[1].strip()
                events_received.append(ev)

        # 2 iteraciones → 2 calls a Anthropic
        assert len(fake.messages.calls) == 2
        # Eventos esperados: meta, stage_progress, agent_thought (de la tool), stage_progress, done
        assert "meta" in events_received
        assert "agent_thought" in events_received
        assert "done" in events_received


class TestModelSelector:
    @pytest.mark.asyncio
    async def test_sonnet_default(self, simple_end_turn_response):
        fake = FakeAnthropic(simple_end_turn_response)
        orch = LeanOrchestrator(
            anthropic_client=fake, pool=None,
            firm_id=uuid4(), user_id=uuid4(),
        )
        async for _ in orch.run(intent="X", doc_type_hint="poder_especial"):
            pass
        assert fake.messages.calls[0]["model"] == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_opus_for_complex_doc_types(self, simple_end_turn_response):
        fake = FakeAnthropic(simple_end_turn_response)
        orch = LeanOrchestrator(
            anthropic_client=fake, pool=None,
            firm_id=uuid4(), user_id=uuid4(),
        )
        async for _ in orch.run(intent="X", doc_type_hint="concepto_juridico"):
            pass
        assert fake.messages.calls[0]["model"] == "claude-opus-4-7"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("doc_type", sorted(OPUS_DOC_TYPES))
    async def test_all_opus_doc_types_use_opus(self, doc_type, simple_end_turn_response):
        fake = FakeAnthropic(simple_end_turn_response)
        orch = LeanOrchestrator(
            anthropic_client=fake, pool=None,
            firm_id=uuid4(), user_id=uuid4(),
        )
        async for _ in orch.run(intent="X", doc_type_hint=doc_type):
            pass
        assert fake.messages.calls[0]["model"] == "claude-opus-4-7"


class TestPromptCaching:
    @pytest.mark.asyncio
    async def test_cache_control_in_system_prompt(self, simple_end_turn_response):
        fake = FakeAnthropic(simple_end_turn_response)
        orch = LeanOrchestrator(
            anthropic_client=fake, pool=None,
            firm_id=uuid4(), user_id=uuid4(),
        )
        async for _ in orch.run(intent="X"):
            pass
        call = fake.messages.calls[0]
        # system es lista con cache_control ephemeral
        assert isinstance(call["system"], list)
        assert call["system"][0].get("cache_control") == {"type": "ephemeral"}

    @pytest.mark.asyncio
    async def test_beta_header_sent(self, simple_end_turn_response):
        fake = FakeAnthropic(simple_end_turn_response)
        orch = LeanOrchestrator(
            anthropic_client=fake, pool=None,
            firm_id=uuid4(), user_id=uuid4(),
        )
        async for _ in orch.run(intent="X"):
            pass
        call = fake.messages.calls[0]
        assert "extra_headers" in call
        assert "prompt-caching" in call["extra_headers"].get("anthropic-beta", "")


class TestSafetyLimits:
    @pytest.mark.asyncio
    async def test_max_iterations_safety(self):
        """Si Anthropic siempre devuelve tool_use sin end_turn, el loop se corta."""
        # Crear 32 respuestas tool_use seguidas (más allá del max_iterations=30)
        infinite_loop = [
            FakeResponse(
                stop_reason="tool_use",
                content=[FakeBlock(type="tool_use", id=f"tu-{i}",
                                   name="narrate_progress",
                                   input={"message": f"iter {i}", "kind": "narration"})],
            )
            for i in range(50)
        ]
        fake = FakeAnthropic(infinite_loop)
        orch = LeanOrchestrator(
            anthropic_client=fake, pool=None,
            firm_id=uuid4(), user_id=uuid4(),
        )
        events_received: list[str] = []
        async for sse_bytes in orch.run(intent="loop infinito"):
            text = sse_bytes.decode("utf-8")
            if text.startswith("event:"):
                ev = text.split("\n")[0].split(":", 1)[1].strip()
                events_received.append(ev)
        # Debe haber emitido error y parado
        assert "error" in events_received
        # No más de max_iterations llamadas (30 default)
        assert len(fake.messages.calls) <= 30


class TestRegistrySchemaPassedToAnthropic:
    @pytest.mark.asyncio
    async def test_tools_schema_sent(self, simple_end_turn_response):
        fake = FakeAnthropic(simple_end_turn_response)
        orch = LeanOrchestrator(
            anthropic_client=fake, pool=None,
            firm_id=uuid4(), user_id=uuid4(),
        )
        async for _ in orch.run(intent="X"):
            pass
        call = fake.messages.calls[0]
        tools = call.get("tools", [])
        assert len(tools) == 18
        names = {t["name"] for t in tools}
        assert "verify_citation" in names
        assert "generate_clause" in names
        assert "build_docx" in names
