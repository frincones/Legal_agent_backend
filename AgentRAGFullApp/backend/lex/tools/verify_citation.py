"""Tool 6 · verify_citation · wrap VerificationAgent + 4-tier mapping.

CRÍTICO. Esta tool es el corazón del agente: garantiza que toda cita
normativa o jurisprudencial citada en el documento existe, está vigente y
tiene fuente_url canónica. El Brain la invoca por cada referencia legal.

Mapeo estado → 4-tier:
  verificada + no derogada    → GROUNDED
  verificada + derogada       → DEROGADA
  superada                    → DEROGADA
  sospechosa                  → VERIFY_FLAG
  no_encontrada               → NOT_FOUND
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from lex.verify.verification_agent import VerificationAgent

from .base import ToolContext, ToolDef, ToolError

logger = logging.getLogger(__name__)


def _estado_to_tier(estado: str, derogada: bool, modulada: bool = False) -> str:
    """M20.13: agregamos 5° tier MODULADA (norma vigente pero modulada
    por jurisprudencia constitucional - aplicable con limitaciones)."""
    if estado == "no_encontrada":
        return "NOT_FOUND"
    if estado == "superada" or derogada:
        return "DEROGADA"
    if estado == "modulada" or modulada:
        return "MODULADA"
    if estado == "sospechosa":
        return "VERIFY_FLAG"
    if estado == "verificada":
        return "GROUNDED"
    return "VERIFY_FLAG"


class VerifyCitationTool(ToolDef):
    name = "verify_citation"
    description = (
        "Verifica una cita normativa o jurisprudencial colombiana. Retorna el "
        "tier (GROUNDED / VERIFY_FLAG / DEROGADA / NOT_FOUND), fuente_url oficial, "
        "y sugerencia de corrección si la cita no existe o está derogada. "
        "Llamar por CADA cita que el documento contenga (puede invocarse en paralelo "
        "para múltiples citas)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "citation": {"type": "string", "description": "Texto literal de la cita, e.g. 'Arts. 2142 ss. CC' o 'T-067/2025'"},
            "kind": {
                "type": "string",
                "enum": ["norma", "jurisprudencia"],
                "default": "norma",
            },
        },
        "required": ["citation"],
    }
    invokes_llm = True   # internamente verifier puede llamar LLM normalizer
    cacheable = True
    cache_ttl_seconds = 86400   # 24h
    timeout_seconds = 30.0

    def __init__(self, pool=None, anthropic_client=None, openai_client=None, **_: Any):
        self.pool = pool
        # M20.14 fix: VerificationAgent → JudgeAgent + llm_normalizer usan API
        # estilo OpenAI (client.chat.completions.create) sobre gpt-4o-mini.
        # Si recibe AsyncAnthropic falla con AttributeError. Preferir OpenAI.
        self.client = openai_client or anthropic_client

    async def run(
        self,
        ctx: ToolContext,
        citation: str,
        kind: str = "norma",
    ) -> dict:
        # M20.14 fix: igual razón — preferir OpenAI sobre Anthropic.
        client = self.client or ctx.openai_client or ctx.anthropic_client
        pool = self.pool or ctx.pool
        if pool is None:
            # M20.13: degradación graceful (dev/test sin BD): heurística sólo.
            # Permite que el Brain razone aunque no haya verificación contra fuente.
            from lex.verify.derogation_detector import (
                detect_explicit_derogation,
                detect_modulation,
            )
            deg = detect_explicit_derogation(citation)
            mod = detect_modulation(citation)
            if deg is not None:
                tier = "DEROGADA"
                suggested = deg.derogada_por
            elif mod is not None:
                tier = "MODULADA"
                suggested = mod.modulada_por
            else:
                tier = "VERIFY_FLAG"
                suggested = None
            logger.info("verify_citation sin pool · degradado a %s para %r", tier, citation)
            return {
                "citation": citation, "kind": kind, "tier": tier,
                "exists": tier != "VERIFY_FLAG",
                "verified": False, "vigente": tier not in ("DEROGADA",),
                "fuente_url_oficial": None,
                "suggested_correction": suggested,
                "method": "heuristic_no_pool",
                "confidence": 0.9 if tier in ("DEROGADA", "MODULADA") else 0.0,
                "estado_legacy": "sospechosa" if tier == "VERIFY_FLAG" else "verificada",
                "audit": {"_warning": "pool no disponible · heurística pura"},
            }

        agent = VerificationAgent(
            client=client,
            pool=pool,
            firm_id=str(ctx.firm_id) if ctx.firm_id else None,
            user_id=str(ctx.user_id) if ctx.user_id else None,
        )
        verdict = await agent.verify(citation, kind)

        audit = verdict.to_audit_dict() if hasattr(verdict, "to_audit_dict") else {}
        # M20.13: detectar modulación vía legal_note (e.g., "modulada por C-XXX/YY")
        legal_note = getattr(verdict, "legal_note", "") or ""
        is_modulada = any(kw in legal_note.lower() for kw in ["modulad", "exequibilidad condicionad"])
        tier = _estado_to_tier(
            verdict.estado,
            bool(getattr(verdict, "derogada", False)),
            modulada=is_modulada,
        )

        return {
            "citation": citation,
            "kind": kind,
            "tier": tier,
            "exists": verdict.verified or tier in ("DEROGADA", "VERIFY_FLAG"),
            "verified": bool(verdict.verified),
            "vigente": not bool(getattr(verdict, "derogada", False)),
            "fuente_url_oficial": verdict.fuente_url,
            "fuente_url_original": getattr(verdict, "fuente_url_original", None),
            "fuente_url_vigente": getattr(verdict, "fuente_url_vigente", None),
            "url_validated": bool(getattr(verdict, "url_validated", False)),
            "suggested_correction": getattr(verdict, "suggested_correction", None),
            "legal_note": getattr(verdict, "legal_note", None),
            "method": verdict.method,
            "confidence": float(verdict.confidence or 0.0),
            "estado_legacy": verdict.estado,
            "audit": audit,
        }


def build_tool(pool=None, anthropic_client=None, openai_client=None, **_: Any) -> ToolDef:
    return VerifyCitationTool(pool=pool, anthropic_client=anthropic_client, openai_client=openai_client)
