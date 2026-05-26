"""BaseTool interface — contrato uniforme para todos los tools del VerificationAgent."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, Optional


ToolStatus = Literal["hit", "miss", "error", "timeout"]


@dataclass
class ToolResult:
    """Resultado uniforme de cualquier tool."""
    tool_name: str
    status: ToolStatus
    confidence: float = 0.0  # 0.0-1.0
    fuente_url: Optional[str] = None
    titulo: Optional[str] = None
    chunk_id: Optional[str] = None
    raw_evidence: dict = field(default_factory=dict)
    duration_ms: int = 0
    error_message: Optional[str] = None

    @property
    def is_hit(self) -> bool:
        return self.status == "hit"

    # M18: provenance helpers (extraen campos de raw_evidence)
    @property
    def discovered_by(self) -> str:
        """Origen del hit. Default a tool_name si no especificado."""
        ev = self.raw_evidence or {}
        return ev.get("discovered_by") or self.tool_name

    @property
    def snippet(self) -> Optional[str]:
        """Snippet textual que confirma la cita (si tool lo extrajo)."""
        ev = self.raw_evidence or {}
        return ev.get("snippet")

    @property
    def query_used(self) -> Optional[str]:
        """Query enviado al search engine (si aplica)."""
        ev = self.raw_evidence or {}
        return ev.get("query") or ev.get("query_used")


class BaseTool(ABC):
    """Interfaz uniforme. Subclases implementan `run()`."""

    name: str = "base"
    timeout_seconds: float = 8.0
    max_retries: int = 1

    def __init__(self, pool=None, client=None):
        self.pool = pool
        self.client = client

    @abstractmethod
    async def run(self, parsed) -> ToolResult:
        """Ejecuta el tool y retorna ToolResult.

        parsed: ParsedCitation (de utils.citation_verifier)
        """
        ...

    def _build_miss(self, reason: str = "not_found") -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            status="miss",
            confidence=0.0,
            raw_evidence={"reason": reason},
        )

    def _build_error(self, error: Exception) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            status="error",
            confidence=0.0,
            error_message=str(error)[:200],
        )

    def _ensure_fuente_url(self, result: ToolResult, parsed) -> ToolResult:
        """M17: garantiza fuente_url si el tool hit pero no asignó URL.

        Llamado por subclases al final de run() para garantizar el contrato:
        "todo tool hit DEBE tener fuente_url no null".
        """
        if result.status == "hit" and not result.fuente_url:
            try:
                from utils.citation_url_builder import build_canonical_url
                result.fuente_url = build_canonical_url(parsed)
            except Exception:
                pass
        return result
