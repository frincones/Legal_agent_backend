"""Sprint M18 · JudgeAgent — verificador adversarial del verdict.

Después de que `EvidenceAccumulator` construye el verdict tentativo, el
JudgeAgent (LLM gpt-4o-mini @ temp=0) revisa la evidencia contra una
checklist explícita y decide:

  - accept   → verdict OK, persistir
  - refine   → re-búsqueda con next_query (max 1 retry)
  - reject   → verdict.estado = "no_encontrada" con explicación

Costo: ~$0.0001 por call (gpt-4o-mini, ~500 tokens in / 100 out).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

JUDGE_MODEL = "gpt-4o-mini"
JUDGE_MAX_TOKENS = 200
JUDGE_TEMPERATURE = 0.0


JUDGE_SYSTEM_PROMPT = """Eres un auditor adversarial de citas legales colombianas.
Tu trabajo es VALIDAR si el verdict propuesto por el sistema de verificación es defendible o tiene gaps.

CHECKLIST (todos deben pasar para sufficient=true):
1. La URL apunta a un dominio oficial gov.co
2. El título/snippet del resultado coincide con la cita (no es página genérica de error)
3. Al menos un tool retornó evidencia (snippet, título o chunk) que confirma la cita
4. No hay contradicción mayor entre tools (BD dice X, search dice Y)
5. Si es jurisprudencia: corte correcta + tipo válido + año plausible
6. Si es norma (ley/decreto): número y año coinciden con la evidencia

ACCIONES:
- "accept"  : verdict es defendible. Cierra la verificación.
- "refine"  : evidencia insuficiente pero hay info útil. Sugiere next_query para re-buscar.
- "reject"  : evidencia contradictoria o nula. Marcar como no_encontrada.

Output JSON estricto:
{
  "sufficient": true|false,
  "confidence_adjusted": 0.0-1.0,
  "missing": ["..."],
  "action": "accept"|"refine"|"reject",
  "next_query": "..." | null,
  "rationale": "1-2 frases explicando la decisión"
}

Responde SOLO con el JSON. Sin markdown, sin comentarios."""


@dataclass
class JudgeOutput:
    """Output estructurado del Judge."""
    sufficient: bool
    confidence_adjusted: float
    missing: list[str] = field(default_factory=list)
    action: str = "accept"  # accept|refine|reject
    next_query: Optional[str] = None
    rationale: str = ""
    raw_response: Optional[str] = None
    duration_ms: int = 0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "sufficient": self.sufficient,
            "confidence_adjusted": self.confidence_adjusted,
            "missing": self.missing,
            "action": self.action,
            "next_query": self.next_query,
            "rationale": self.rationale,
        }


def _summarize_tool_results(tool_results: list) -> list[dict]:
    """Resume tool_results a campos mínimos para el prompt (ahorro tokens)."""
    summaries = []
    for t in tool_results[:6]:  # max 6 tools
        if not t:
            continue
        ev = t.raw_evidence or {}
        snippet = ev.get("snippet") or ""
        summaries.append({
            "tool": t.tool_name,
            "status": t.status,
            "confidence": round(t.confidence, 2),
            "fuente_url": (t.fuente_url or "")[:150],
            "titulo": (t.titulo or "")[:120],
            "snippet": snippet[:200] if snippet else "",
            "discovered_by": ev.get("discovered_by", ""),
        })
    return summaries


class JudgeAgent:
    """Adversarial validator usando LLM."""

    def __init__(self, client, enabled: Optional[bool] = None):
        """
        Args:
            client: openai AsyncClient (gpt-4o-mini)
            enabled: bypass si False (útil para tests). None=auto (True si client OK).
        """
        self.client = client
        if enabled is None:
            self.enabled = client is not None
        else:
            self.enabled = enabled

    async def judge(
        self,
        citation_text: str,
        parsed,
        verdict,
        tool_results: list,
    ) -> JudgeOutput:
        """Evalúa verdict tentativo.

        Returns JudgeOutput. En caso de error LLM, retorna accept por defecto
        (no bloquear el flow del usuario).
        """
        started = time.time()

        if not self.enabled or not self.client:
            return JudgeOutput(
                sufficient=True,
                confidence_adjusted=verdict.confidence,
                action="accept",
                rationale="judge_disabled",
                duration_ms=int((time.time() - started) * 1000),
            )

        # Construir prompt
        try:
            tool_summaries = _summarize_tool_results(tool_results)
            user_prompt = {
                "cita": citation_text,
                "parsed": {
                    "kind": parsed.kind,
                    "tipo": parsed.tipo,
                    "numero": parsed.numero,
                    "anio": parsed.anio,
                    "normalized": getattr(parsed, "normalized", None),
                },
                "verdict_tentativo": {
                    "estado": verdict.estado,
                    "confidence": round(verdict.confidence, 3),
                    "method": verdict.method,
                    "fuente_url": (verdict.fuente_url or "")[:150],
                    "titulo": (verdict.titulo or "")[:120],
                },
                "tool_results": tool_summaries,
            }
        except Exception as e:
            logger.warning("judge prompt build failed: %s", e)
            return JudgeOutput(
                sufficient=True,
                confidence_adjusted=verdict.confidence,
                action="accept",
                rationale="prompt_build_error",
                duration_ms=int((time.time() - started) * 1000),
                error=str(e),
            )

        # Llamar LLM
        try:
            resp = await self.client.chat.completions.create(
                model=JUDGE_MODEL,
                messages=[
                    {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
                ],
                temperature=JUDGE_TEMPERATURE,
                max_tokens=JUDGE_MAX_TOKENS,
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content or "{}"
        except Exception as e:
            # No bloquear: fallback accept con confidence sin ajustar
            logger.warning("judge LLM call failed: %s", e)
            return JudgeOutput(
                sufficient=True,
                confidence_adjusted=verdict.confidence,
                action="accept",
                rationale=f"llm_error:{type(e).__name__}",
                duration_ms=int((time.time() - started) * 1000),
                error=str(e)[:200],
            )

        # Parse JSON output
        try:
            parsed_out = json.loads(raw)
            action = parsed_out.get("action", "accept")
            if action not in ("accept", "refine", "reject"):
                action = "accept"
            conf_adj = parsed_out.get("confidence_adjusted", verdict.confidence)
            try:
                conf_adj = float(conf_adj)
            except (TypeError, ValueError):
                conf_adj = verdict.confidence
            conf_adj = max(0.0, min(1.0, conf_adj))

            next_query = parsed_out.get("next_query")
            if next_query and not isinstance(next_query, str):
                next_query = None

            missing = parsed_out.get("missing", [])
            if not isinstance(missing, list):
                missing = []

            return JudgeOutput(
                sufficient=bool(parsed_out.get("sufficient", True)),
                confidence_adjusted=conf_adj,
                missing=[str(m)[:80] for m in missing[:5]],
                action=action,
                next_query=next_query[:300] if next_query else None,
                rationale=str(parsed_out.get("rationale", ""))[:400],
                raw_response=raw[:1000],
                duration_ms=int((time.time() - started) * 1000),
            )
        except Exception as e:
            logger.warning("judge JSON parse failed: %s; raw=%s", e, raw[:200])
            return JudgeOutput(
                sufficient=True,
                confidence_adjusted=verdict.confidence,
                action="accept",
                rationale="json_parse_error",
                raw_response=raw[:500],
                duration_ms=int((time.time() - started) * 1000),
                error=str(e),
            )
