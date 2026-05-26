"""Sprint M18.d / M19.5 · Pre-flight sanity check del prompt del usuario.

Antes de generar el documento, hacemos UN LLM call a gpt-4o-mini que
detecta errores legales OBVIOS en el prompt y los reporta al usuario
como `agent_thought` warnings.

Ejemplos de detecciones (estilo Claude):
- "salario integral $4.850.000" → imposible (debe ser ≥13 SMMLV)
- "Ley 1280 de 2009 (madre cabeza familia)" → error, esa ley es licencia por luto
- "SU-440 de 2021 (estabilidad laboral)" → error, esa sentencia es identidad de género
- "Decreto 2351 de 1965 vigente como CST original" → modificaciones aplicadas

NO bloquea la generación — solo advierte. El usuario decide si corrige.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

PREFLIGHT_MODEL = "gpt-4o-mini"
PREFLIGHT_MAX_TOKENS = 400
PREFLIGHT_TEMPERATURE = 0.0


PREFLIGHT_SYSTEM_PROMPT = """Eres un abogado senior colombiano que REVISA prompts antes de redactar documentos legales.

Tu trabajo: detectar ERRORES JURÍDICOS OBVIOS en la solicitud del usuario y advertir al sistema.

QUÉ BUSCAR:
1. CITAS INCORRECTAS conocidas (sentencias mal atribuidas, leyes confundidas)
   Ej.: "SU-440/2021 sobre estabilidad laboral" (esa es de identidad de género)
   Ej.: "Ley 1280/2009 sobre madre cabeza" (esa es licencia por luto; la correcta es Ley 82/1993 + Ley 1232/2008)

2. INCONSISTENCIAS DE HECHOS Y NORMAS
   Ej.: "salario integral $4.850.000" → imposible, salario integral debe ser ≥13 SMMLV (~$18.5M en 2025)
   Ej.: "indemnización por despido sin justa causa por 6 meses" (CST art. 64 establece otra fórmula)

3. NORMAS DEROGADAS/MODIFICADAS importantes
   Ej.: "Ley 1010/2006 art 18: caducidad 6 meses" → modificado por Ley 2209/2022 (ahora 3 años)
   Ej.: "Decreto 2351/1965 vigente" → su mayoría fue subrogado por Ley 50/1990 + Ley 789/2002

4. FECHAS/PLAZOS IMPOSIBLES o INCOHERENTES
   Ej.: prescripción ya vencida, fechas posteriores a hoy, periodos imposibles

NO BUSQUES:
- Errores ortográficos o de estilo
- Falta de detalles (eso lo extrae después)
- Errores menores de formato

ACCIONES POR HALLAZGO:
- "warning": problema serio que el usuario debe saber (mostrar warning)
- "info": observación útil (mostrar nota)
- "blocker": problema que impide generar (raro, casi nunca)

Output JSON estricto:
{
  "findings": [
    {
      "severity": "warning"|"info"|"blocker",
      "issue": "descripción del problema (1-2 frases)",
      "suggestion": "qué hacer en su lugar"
    }
  ],
  "overall_assessment": "1 frase resumen general"
}

Si NO hay errores: {"findings": [], "overall_assessment": "Prompt sin errores legales detectables"}

Responde SOLO con el JSON. Sin markdown."""


@dataclass
class PreflightFinding:
    severity: str  # warning|info|blocker
    issue: str
    suggestion: Optional[str] = None


@dataclass
class PreflightResult:
    findings: list[PreflightFinding] = field(default_factory=list)
    overall_assessment: str = ""
    duration_ms: int = 0
    error: Optional[str] = None
    raw_response: Optional[str] = None

    @property
    def has_blockers(self) -> bool:
        return any(f.severity == "blocker" for f in self.findings)

    @property
    def has_warnings(self) -> bool:
        return any(f.severity == "warning" for f in self.findings)


async def preflight_check(
    client,
    intent: str,
    user_brief: str = "",
    enabled: Optional[bool] = None,
) -> PreflightResult:
    """Pre-flight sanity check del prompt del usuario.

    Args:
        client: openai AsyncClient
        intent: prompt principal del usuario
        user_brief: contexto adicional opcional
        enabled: None=auto (True si client OK). False para bypass.

    Returns PreflightResult con findings + assessment.
    En caso de error LLM: retorna result vacío sin findings (no bloquea).
    """
    started = time.time()

    if enabled is False or client is None:
        return PreflightResult(
            overall_assessment="preflight_disabled",
            duration_ms=int((time.time() - started) * 1000),
        )

    # Concatenar prompt completo (truncado para ahorrar tokens)
    full_prompt = intent[:3000]
    if user_brief:
        full_prompt += "\n\n--- BRIEF ADICIONAL ---\n" + user_brief[:2000]

    try:
        resp = await client.chat.completions.create(
            model=PREFLIGHT_MODEL,
            messages=[
                {"role": "system", "content": PREFLIGHT_SYSTEM_PROMPT},
                {"role": "user", "content": full_prompt},
            ],
            temperature=PREFLIGHT_TEMPERATURE,
            max_tokens=PREFLIGHT_MAX_TOKENS,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
    except Exception as e:
        logger.warning("preflight LLM call failed: %s", e)
        return PreflightResult(
            overall_assessment=f"preflight_error:{type(e).__name__}",
            duration_ms=int((time.time() - started) * 1000),
            error=str(e)[:200],
        )

    try:
        parsed = json.loads(raw)
        findings_raw = parsed.get("findings", [])
        if not isinstance(findings_raw, list):
            findings_raw = []

        findings: list[PreflightFinding] = []
        for f in findings_raw[:10]:  # cap 10 hallazgos
            if not isinstance(f, dict):
                continue
            sev = f.get("severity", "info")
            if sev not in ("warning", "info", "blocker"):
                sev = "info"
            issue = str(f.get("issue", ""))[:400]
            if not issue:
                continue
            sugg = f.get("suggestion")
            findings.append(PreflightFinding(
                severity=sev,
                issue=issue,
                suggestion=str(sugg)[:300] if sugg else None,
            ))

        return PreflightResult(
            findings=findings,
            overall_assessment=str(parsed.get("overall_assessment", ""))[:300],
            duration_ms=int((time.time() - started) * 1000),
            raw_response=raw[:1500],
        )
    except Exception as e:
        logger.warning("preflight JSON parse failed: %s", e)
        return PreflightResult(
            overall_assessment="parse_error",
            duration_ms=int((time.time() - started) * 1000),
            error=str(e),
            raw_response=raw[:500],
        )
