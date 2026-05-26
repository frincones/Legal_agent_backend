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


JUDGE_SYSTEM_PROMPT = """Eres un auditor experto en derecho colombiano que VALIDA si una cita legal está bien sustentada por la evidencia de las tools.

REGLAS CRITICAS (lee con cuidado, evita falsos rechazos):

1. CODIGOS NO TIENEN AÑO. CST, C.P., C.C., C.CO., CN/CONSTITUCION, CGP, CPACA, CPP, CPP son códigos.
   NO rechazar por "falta año" o "falta numero ley". Son códigos consolidados.

2. CUALQUIER dominio que termine en .gov.co es OFICIAL. Acepta como válido:
   funcionpublica.gov.co, secretariasenado.gov.co, corteconstitucional.gov.co,
   cortesuprema.gov.co, ramajudicial.gov.co, suin-juriscol.gov.co, icbf.gov.co,
   alcaldiabogota.gov.co, cancilleria.gov.co, mintrabajo.gov.co, dian.gov.co,
   minsalud.gov.co, mineducacion.gov.co, etc. TODOS son oficiales.

3. Para JURISPRUDENCIA, basta con que la URL apunte al sitio oficial de la corte
   AUN SI es página de búsqueda/relatoria. Ejemplo: cortesuprema.gov.co/sala-laboral-relatoria
   es válida para confirmar existencia de la sentencia. NO rechaces por "no es el PDF".

4. Si el snippet menciona la sentencia/norma (aunque sea de pasada), HAY evidencia.

5. La URL fue VALIDADA con HTTP 200 + body check (la tool ya lo verificó). Confía en eso.

SUGERENCIA DE CORRECCIONES (NUEVO - importante):
Si DETECTAS que la cita parece INCORRECTA pero podrías sugerir la cita correcta,
pon `suggested_correction` con la cita correcta. Ejemplos:
  - "SU-440/2021" no es sobre estabilidad laboral (es de identidad de género).
    suggested_correction: "SU-049/2017" o "SU-087/2022".
  - "Ley 1280 de 2009" es licencia por luto, NO madre cabeza familia.
    suggested_correction: "Ley 82 de 1993" + "Ley 1232 de 2008".
Esto NO bloquea la verificación. El usuario verá la sugerencia en el AuditPanel.

ACCIONES:
- "accept": evidencia suficiente. Por defecto. Usa esto en 90% de los casos.
- "refine": SOLO si la URL devuelta NO tiene relación alguna con la cita Y sabes mejor query.
- "reject": SOLO si la cita es claramente inexistente o fabricada (raro).

Output JSON estricto:
{
  "sufficient": true|false,
  "confidence_adjusted": 0.0-1.0,
  "action": "accept"|"refine"|"reject",
  "next_query": "..." | null,
  "rationale": "1-2 frases concisas",
  "suggested_correction": "cita corregida si aplica" | null,
  "legal_note": "nota sobre vigencia/modificaciones si aplica" | null
}

Responde SOLO con el JSON, sin markdown, sin comentarios fuera del JSON.
Por defecto sé permisivo (action=accept) — solo rechaza con evidencia clara."""


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
    # M18.c: capacidades nuevas estilo Claude
    suggested_correction: Optional[str] = None  # "SU-440/2021" → "SU-087/2022"
    legal_note: Optional[str] = None            # "Ley 1010 art 18 modificado por Ley 2209/2022"

    def to_dict(self) -> dict:
        return {
            "sufficient": self.sufficient,
            "confidence_adjusted": self.confidence_adjusted,
            "missing": self.missing,
            "action": self.action,
            "next_query": self.next_query,
            "rationale": self.rationale,
            "suggested_correction": self.suggested_correction,
            "legal_note": self.legal_note,
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

            suggested_corr = parsed_out.get("suggested_correction")
            if suggested_corr and not isinstance(suggested_corr, str):
                suggested_corr = None
            legal_note = parsed_out.get("legal_note")
            if legal_note and not isinstance(legal_note, str):
                legal_note = None

            return JudgeOutput(
                sufficient=bool(parsed_out.get("sufficient", True)),
                confidence_adjusted=conf_adj,
                missing=[str(m)[:80] for m in missing[:5]],
                action=action,
                next_query=next_query[:300] if next_query else None,
                rationale=str(parsed_out.get("rationale", ""))[:400],
                suggested_correction=suggested_corr[:200] if suggested_corr else None,
                legal_note=legal_note[:400] if legal_note else None,
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
