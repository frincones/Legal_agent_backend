"""EvidenceAccumulator — combina resultados de tools en un verdict final.

Reglas críticas (del arquitecto):
- NUNCA verificada solo por embedding similarity
- Mínimo 1 fuente autoritativa para verificada (BD cache, live fetch URL, RSS exact)
- mention CSJ → confidence max 0.7 (sospechosa, no verificada)
- Discrepancia entre fuentes → gana la más reciente / mayor confidence
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from lex.verify.tools.base import ToolResult


# Threshold de confidence → estado
THRESHOLD_VERIFICADA = 0.90
THRESHOLD_SOSPECHOSA = 0.70


@dataclass
class EvidenceSet:
    """Conjunto de evidencia recolectada de las tools."""
    citation_text: str
    tool_results: list[ToolResult] = field(default_factory=list)

    def hits(self) -> list[ToolResult]:
        return [t for t in self.tool_results if t.is_hit]

    def best_confidence(self) -> float:
        if not self.hits():
            return 0.0
        return max(t.confidence for t in self.hits())

    def best_hit(self) -> Optional[ToolResult]:
        if not self.hits():
            return None
        return max(self.hits(), key=lambda t: t.confidence)

    def derogada(self) -> bool:
        """True si CheckDerogation reportó derogada."""
        for t in self.tool_results:
            if t.tool_name == "check_derogation":
                vigente = t.raw_evidence.get("vigente", True)
                if not vigente:
                    return True
        return False


@dataclass
class VerificationVerdict:
    """Veredicto final para una cita."""
    citation_text: str
    citation_type: str
    estado: Literal["verificada", "superada", "sospechosa", "no_encontrada"]
    verified: bool
    confidence: float
    method: str  # 'csj_rss_exact', 'corte_cc', 'cache', etc.
    fuente_url: Optional[str] = None
    titulo: Optional[str] = None
    chunk_id: Optional[str] = None
    derogada: bool = False
    sources_tried: list[str] = field(default_factory=list)
    duration_ms: int = 0
    similarity: Optional[float] = None
    # M17: garantía de fuente_url + derogada con 2 links
    fuente_url_original: Optional[str] = None  # la norma citada (puede ser derogada)
    fuente_url_vigente: Optional[str] = None   # la que reemplaza (solo si superada)
    url_http_status: Optional[int] = None       # 200, 404, etc.
    url_validated: bool = False                 # HEAD check pasó
    # M18: provenance + Judge
    discovered_by: Optional[str] = None         # 'brave_search'|'internal_db'|'pattern'|...
    snippet: Optional[str] = None               # evidencia mostrada al usuario
    judge_action: Optional[str] = None          # 'accept'|'refine'|'reject'
    judge_rationale: Optional[str] = None       # explicación auditable
    judge_retried: bool = False                 # si JudgeAgent forzó refine
    query_used: Optional[str] = None            # query enviado a search engine
    # M18.c: capacidades nuevas
    suggested_correction: Optional[str] = None  # "SU-440/21" → "SU-087/22"
    legal_note: Optional[str] = None            # "modificado por Ley 2209/2022"

    def to_audit_dict(self) -> dict:
        """Schema compatible con `verification_results` del orchestrator."""
        return {
            "ref": self.citation_text,
            "type": self.citation_type,
            "verified": self.verified,
            "chunk_id": self.chunk_id,
            "similarity": self.similarity or self.confidence,
            "estado": self.estado,
            "method": self.method,
            "fuente_url": self.fuente_url,
            "fuente_url_original": self.fuente_url_original,
            "fuente_url_vigente": self.fuente_url_vigente,
            "url_http_status": self.url_http_status,
            "url_validated": self.url_validated,
            "is_derogada": self.derogada,
            "titulo": self.titulo,
            # M18: provenance + judge
            "discovered_by": self.discovered_by,
            "snippet": self.snippet,
            "judge_action": self.judge_action,
            "judge_rationale": self.judge_rationale,
            "judge_retried": self.judge_retried,
            "query_used": self.query_used,
            # M18.c: smart corrections
            "suggested_correction": self.suggested_correction,
            "legal_note": self.legal_note,
        }


class EvidenceAccumulator:
    """Combina ToolResults en VerificationVerdict."""

    def collect(self, citation_text: str, tool_results: list[ToolResult]) -> EvidenceSet:
        return EvidenceSet(citation_text=citation_text, tool_results=tool_results)

    def compute_verdict(
        self,
        evidence: EvidenceSet,
        citation_type: str = "norma",
    ) -> VerificationVerdict:
        """Aplica reglas para determinar estado final."""
        sources = [t.tool_name for t in evidence.tool_results]
        total_duration = sum(t.duration_ms for t in evidence.tool_results)

        best_hit = evidence.best_hit()
        confidence = evidence.best_confidence()

        # Bonus si múltiples corroboradores (max 1.0)
        hits_count = len(evidence.hits())
        if hits_count >= 2:
            confidence = min(1.0, confidence + 0.02 * (hits_count - 1))

        # Caso 1: derogada (existe pero no vigente)
        if evidence.derogada() and best_hit is not None:
            return VerificationVerdict(
                citation_text=evidence.citation_text,
                citation_type=citation_type,
                estado="superada",
                verified=True,
                confidence=confidence,
                method=best_hit.tool_name,
                fuente_url=best_hit.fuente_url,
                titulo=best_hit.titulo,
                chunk_id=best_hit.chunk_id,
                derogada=True,
                sources_tried=sources,
                duration_ms=total_duration,
            )

        # Caso 2: alta confianza → verificada
        if confidence >= THRESHOLD_VERIFICADA and best_hit is not None:
            return VerificationVerdict(
                citation_text=evidence.citation_text,
                citation_type=citation_type,
                estado="verificada",
                verified=True,
                confidence=confidence,
                method=best_hit.tool_name,
                fuente_url=best_hit.fuente_url,
                titulo=best_hit.titulo,
                chunk_id=best_hit.chunk_id,
                sources_tried=sources,
                duration_ms=total_duration,
            )

        # Caso 3: confianza media → sospechosa
        if confidence >= THRESHOLD_SOSPECHOSA and best_hit is not None:
            return VerificationVerdict(
                citation_text=evidence.citation_text,
                citation_type=citation_type,
                estado="sospechosa",
                verified=False,
                confidence=confidence,
                method=best_hit.tool_name,
                fuente_url=best_hit.fuente_url,
                titulo=best_hit.titulo,
                chunk_id=best_hit.chunk_id,
                sources_tried=sources,
                duration_ms=total_duration,
            )

        # Caso 4: ninguna fuente confirmó → no encontrada (alucinación)
        return VerificationVerdict(
            citation_text=evidence.citation_text,
            citation_type=citation_type,
            estado="no_encontrada",
            verified=False,
            confidence=confidence,
            method="no_hit",
            sources_tried=sources,
            duration_ms=total_duration,
        )
