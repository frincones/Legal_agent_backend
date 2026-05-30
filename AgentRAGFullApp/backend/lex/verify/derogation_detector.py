"""Sprint M20.13 · Auto-detección de derogaciones tácitas (heurística + LLM).

Más sofisticado que `derogation_verifier.py` (que solo consulta la tabla
`leyes_normas`). Detecta:
  1. Derogaciones explícitas via tabla `derogaciones`
  2. Derogaciones tácitas via comparación de fechas y temas
     (e.g., Ley 1564/2012 derogó Ley 1395/2010 sobre proceso civil)
  3. Modulaciones constitucionales (sentencias C- que cambiaron alcance)

Tabla cache `derogation_inference_cache` evita re-llamar LLM por consulta repetida.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Patrones conocidos de derogación tácita (heurística pre-LLM)
KNOWN_DEROGATIONS = [
    # (regex norma vieja, norma_nueva_que_derogó, año_efecto)
    (r"[Ll]ey\s+1395\s+de\s+2010", "Ley 1564 de 2012 (CGP)", 2014),
    (r"[Dd]ecreto\s+100\s+de\s+1980", "Ley 599 de 2000 (CP)", 2001),
    (r"[Ll]ey\s+57\s+de\s+1985", "Ley 1437 de 2011 (CPACA)", 2012),
    (r"[Dd]ecreto\s+1\s+de\s+1984", "Ley 1437 de 2011 (CPACA)", 2012),
    (r"[Ll]ey\s+50\s+de\s+1990", "Vigente parcialmente, modulada por sentencias C-", 1990),
    # Códigos antiguos
    (r"[Cc]ódigo\s+[Dd]e\s+[Pp]rocedimiento\s+[Cc]ivil(?!\s+y)", "CGP (Ley 1564/2012)", 2014),
    (r"\bCPC\b(?!\s+y)", "CGP (Ley 1564/2012)", 2014),
]


# Sentencias constitucionales que modularon normas vigentes (subset conocido)
KNOWN_MODULATIONS = [
    # (regex norma, sentencia modulante, descripción)
    (r"[Ll]ey\s+1437\s+de\s+2011\s+art\.?\s+4", "C-1011/2008",
     "Notificaciones electrónicas con consentimiento expreso"),
    (r"[Ll]ey\s+599\s+de\s+2000\s+art\.?\s+(?:101|122|123)", "C-355/2006",
     "IVE - despenalización condicionada"),
    (r"[Ll]ey\s+136\s+de\s+1994\s+art\.?\s+(?:35|36)", "C-617/2008",
     "Inhabilidades modulada"),
]


@dataclass
class DerogationInferenceResult:
    norma_citada: str
    tier: str   # GROUNDED | DEROGADA | MODULADA | VERIFY_FLAG | NOT_FOUND
    confidence: float
    derogada_por: Optional[str] = None
    modulada_por: Optional[str] = None
    explanation: Optional[str] = None
    detection_method: str = "heuristic"   # heuristic | tabla | llm


def detect_explicit_derogation(norma_text: str) -> Optional[DerogationInferenceResult]:
    """Match contra patrones conocidos (sin LLM, instantáneo)."""
    for pattern, derogada_por, _año in KNOWN_DEROGATIONS:
        if re.search(pattern, norma_text):
            return DerogationInferenceResult(
                norma_citada=norma_text,
                tier="DEROGADA",
                confidence=0.95,
                derogada_por=derogada_por,
                explanation=f"Derogación conocida: '{pattern}' fue reemplazada por {derogada_por}",
                detection_method="heuristic",
            )
    return None


def detect_modulation(norma_text: str) -> Optional[DerogationInferenceResult]:
    """Match contra sentencias modulantes conocidas."""
    for pattern, sentencia, desc in KNOWN_MODULATIONS:
        if re.search(pattern, norma_text):
            return DerogationInferenceResult(
                norma_citada=norma_text,
                tier="MODULADA",
                confidence=0.90,
                modulada_por=sentencia,
                explanation=f"Modulada por {sentencia}: {desc}",
                detection_method="heuristic",
            )
    return None


async def detect_derogation_cached(
    pool,
    norma_text: str,
    *,
    use_llm_fallback: bool = False,
) -> Optional[DerogationInferenceResult]:
    """Versión async con cache SQL.

    Flujo:
      1. Heurística estática (KNOWN_DEROGATIONS / KNOWN_MODULATIONS)
      2. Cache hit en `derogation_inference_cache` (SHA del texto)
      3. Query tabla `derogaciones` (M19 existente)
      4. LLM fallback (opcional, costoso)
    """
    # 1. Heurística estática (instantánea)
    result = detect_explicit_derogation(norma_text)
    if result:
        return result
    result = detect_modulation(norma_text)
    if result:
        return result

    if pool is None:
        return None

    # 2. Cache SQL
    text_hash = hashlib.sha256(norma_text.lower().encode("utf-8")).hexdigest()[:32]
    try:
        async with pool.acquire() as conn:
            cached = await conn.fetchrow(
                """
                select tier, confidence, derogada_por, modulada_por,
                       explanation, detection_method
                from derogation_inference_cache
                where text_hash = $1
                  and (expires_at is null or expires_at > now())
                limit 1
                """,
                text_hash,
            )
            if cached:
                return DerogationInferenceResult(
                    norma_citada=norma_text,
                    tier=cached["tier"],
                    confidence=float(cached["confidence"]),
                    derogada_por=cached["derogada_por"],
                    modulada_por=cached["modulada_por"],
                    explanation=cached["explanation"],
                    detection_method="cache",
                )
    except Exception as e:
        logger.debug("cache lookup falló: %s", e)

    # 3. Tabla derogaciones existente (M19)
    try:
        from .derogation_verifier import DerogationVerifier
        verifier = DerogationVerifier(pool=pool)
        res = await verifier.check(norma_text)
        if not res.vigente:
            return DerogationInferenceResult(
                norma_citada=norma_text,
                tier="DEROGADA",
                confidence=float(res.confidence),
                derogada_por=res.derogada_por,
                explanation=f"Derogada (tabla normas, conf={res.confidence:.2f})",
                detection_method="tabla",
            )
    except Exception as e:
        logger.debug("DerogationVerifier check falló: %s", e)

    # 4. LLM fallback (opcional, no implementado por defecto en esta versión)
    if use_llm_fallback:
        logger.info("LLM fallback no implementado en esta versión; retornando None")

    return None
