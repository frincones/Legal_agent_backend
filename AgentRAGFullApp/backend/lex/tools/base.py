"""Sprint M20.02 · ToolDef base · interfaz de tools para Anthropic tool_use.

Cada tool del LeanOrchestrator implementa:
  - name             → identificador único (snake_case)
  - description      → texto que el Brain lee para decidir cuándo usarla
  - input_schema     → JSON Schema con los parámetros (formato Anthropic tool_use)
  - async run(ctx, **kwargs) → ejecuta y retorna ToolResult

NO confundir con lex/verify/tools/base.py — este es el contrato global
de tools del Brain. El verify/tools/base.py es interno del VerificationAgent.
"""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Optional
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)


ToolStatus = Literal["success", "error", "timeout", "cached"]


@dataclass
class ToolContext:
    """Contexto que se pasa a cada tool al invocarla.

    Inyectado por el dispatcher con datos del request actual.
    """
    generation_id: UUID
    firm_id: Optional[UUID]
    user_id: Optional[UUID]
    matter_id: Optional[UUID] = None
    iteration: int = 0
    pool: Any = None              # asyncpg pool
    anthropic_client: Any = None
    openai_client: Any = None
    metadata: dict = field(default_factory=dict)


@dataclass
class ToolCall:
    """Invocación de una tool (tal como Anthropic la emite en tool_use)."""
    tool_use_id: str              # ID que Anthropic genera, se devuelve en tool_result
    tool_name: str
    input: dict = field(default_factory=dict)
    iteration: int = 0            # # iteración del ReAct loop


@dataclass
class ToolResult:
    """Resultado de una tool. Se devuelve al Brain como tool_result."""
    tool_use_id: str
    tool_name: str
    status: ToolStatus
    output: Any = None            # JSON-serializable
    error_class: Optional[str] = None
    error_message: Optional[str] = None
    duration_ms: int = 0
    cached: bool = False
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    cost_usd: Optional[float] = None
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    model_used: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in ("success", "cached")

    def to_anthropic_tool_result(self) -> dict:
        """Formato esperado por Anthropic messages.create() en role=user content[].tool_result.

        M20.14 Camino 3: garantizar content NON-EMPTY siempre. Anthropic API
        rechaza tool_result con content vacio (""/None/[]) — especialmente
        cuando is_error=true necesita un mensaje legible para el modelo.
        """
        if self.status == "success" or self.status == "cached":
            raw = self.output if isinstance(self.output, str) else _safe_json_dumps(self.output)
            content = raw if (raw and raw.strip()) else "(tool returned empty output)"
        else:
            cls = self.error_class or "UnknownError"
            msg = self.error_message or "(no error message)"
            content = f"ERROR ({cls}): {msg}"
        # Cap defensivo a 50K chars — Anthropic rechaza tool_result gigantes
        # y puede inflar mensajes hasta romper context window.
        if len(content) > 50_000:
            content = content[:50_000] + "\n...(truncated)"
        return {
            "type": "tool_result",
            "tool_use_id": self.tool_use_id,
            "content": content,
            "is_error": not self.ok,
        }


class ToolError(Exception):
    """Errores controlados dentro de tools (no rompen el ReAct loop, retornan error tool_result)."""


def _safe_json_dumps(obj: Any) -> str:
    import json
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception as e:
        return f'{{"_serialization_error": "{e}"}}'


class ToolDef(abc.ABC):
    """Interfaz abstracta para una tool registrada en el ToolRegistry."""

    name: str = "base"
    description: str = ""
    input_schema: dict = {"type": "object", "properties": {}, "required": []}

    # ¿esta tool invoca LLM? (afecta el audit + costo)
    invokes_llm: bool = False

    # ¿esta tool es cacheable? (Redis o SQL)
    cacheable: bool = False
    cache_ttl_seconds: int = 3600

    # timeout default (puede sobreescribirse por subclase)
    timeout_seconds: float = 60.0

    @abc.abstractmethod
    async def run(self, ctx: ToolContext, **kwargs: Any) -> Any:
        """Ejecuta la tool con los kwargs validados contra input_schema.

        Retorna el output (cualquier JSON-serializable). El dispatcher se
        encarga de envolverlo en ToolResult + persistir audit.

        Si algo falla:
          - levantar ToolError(...) → dispatcher captura → ToolResult status=error
          - levantar TimeoutError → status=timeout
          - cualquier otra excepción → status=error con error_class=ClassName
        """
        raise NotImplementedError

    def schema_dict(self) -> dict:
        """Formato compatible con Anthropic tools=[...] en messages.create."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def __repr__(self) -> str:
        return f"<Tool name={self.name!r} llm={self.invokes_llm} cache={self.cacheable}>"
