"""Stage 1: Classifier — detecta doc_type + jurisdicción + materia desde intent.

Usa gpt-4o-mini con JSON output (rápido + barato).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Catálogo soportado M1 (M2 extenderá vía TemplateDef registry)
SUPPORTED_DOC_TYPES = [
    "demanda_laboral_ordinaria",
    "demanda_civil_ordinaria",
    "demanda_ejecutivo_singular",
    "demanda_alimentos",
    "tutela",
    "derecho_peticion",
    "contrato_arrendamiento",
    "contrato_prestacion_servicios",
    "denuncia_penal",
    "recurso_apelacion",
    "concepto_juridico",
    "poder_especial",
]


@dataclass
class ClassificationResult:
    doc_type: str
    jurisdiccion: str  # 'laboral' | 'civil' | 'penal' | 'constitucional' | 'administrativo' | 'familia' | 'tributario' | 'comercial' | 'general'
    materia: str
    confidence: float
    reasoning: str = ""


CLASSIFIER_SYSTEM = """Eres un clasificador legal experto en derecho colombiano.
Tu tarea es analizar la intención del usuario y devolver:
1. doc_type (de la lista permitida)
2. jurisdiccion
3. materia (descripción corta de la materia legal)
4. confidence (0.0-1.0)

Responde SIEMPRE en JSON estricto sin texto adicional.

Lista permitida de doc_type:
- demanda_laboral_ordinaria
- demanda_civil_ordinaria
- demanda_ejecutivo_singular
- demanda_alimentos
- tutela
- derecho_peticion
- contrato_arrendamiento
- contrato_prestacion_servicios
- denuncia_penal
- recurso_apelacion
- concepto_juridico
- poder_especial

Jurisdicciones: laboral, civil, penal, constitucional, administrativo, familia, tributario, comercial, general.
"""


async def classify(client, intent: str, brief: str | None = None) -> ClassificationResult:
    """Clasifica el intent del usuario en un doc_type soportado."""
    user_prompt = f"INTENT: {intent}\n\nBRIEF (opcional): {brief or '(sin brief)'}\n\nResponde con JSON: {{\"doc_type\": \"...\", \"jurisdiccion\": \"...\", \"materia\": \"...\", \"confidence\": 0.0-1.0, \"reasoning\": \"...\"}}"

    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": CLASSIFIER_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
            max_tokens=200,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        doc_type = data.get("doc_type", "tutela")
        if doc_type not in SUPPORTED_DOC_TYPES:
            logger.warning("classifier returned unknown doc_type: %s, fallback to tutela", doc_type)
            doc_type = "tutela"
        return ClassificationResult(
            doc_type=doc_type,
            jurisdiccion=data.get("jurisdiccion", "general"),
            materia=data.get("materia", ""),
            confidence=float(data.get("confidence", 0.5)),
            reasoning=data.get("reasoning", ""),
        )
    except Exception as e:
        logger.exception("classifier failed, using fallback: %s", e)
        return _fallback_classify(intent)


def _fallback_classify(intent: str) -> ClassificationResult:
    """Heurística simple si gpt-4o-mini falla."""
    lower = (intent or "").lower()
    if "tutela" in lower or "amparo" in lower:
        return ClassificationResult("tutela", "constitucional", "derechos fundamentales", 0.7)
    if "alimentos" in lower:
        return ClassificationResult("demanda_alimentos", "familia", "fijación cuota alimentaria", 0.7)
    if "ejecutivo" in lower:
        return ClassificationResult("demanda_ejecutivo_singular", "civil", "cobro ejecutivo", 0.7)
    if "laboral" in lower or "despido" in lower or "indemnización" in lower:
        return ClassificationResult("demanda_laboral_ordinaria", "laboral", "despido sin justa causa", 0.7)
    if "demanda" in lower:
        return ClassificationResult("demanda_civil_ordinaria", "civil", "general", 0.6)
    if "petición" in lower or "peticion" in lower:
        return ClassificationResult("derecho_peticion", "administrativo", "petición", 0.7)
    if "denuncia" in lower:
        return ClassificationResult("denuncia_penal", "penal", "general", 0.7)
    if "apelación" in lower or "apelacion" in lower or "recurso" in lower:
        return ClassificationResult("recurso_apelacion", "general", "recurso", 0.6)
    if "concepto" in lower:
        return ClassificationResult("concepto_juridico", "general", "concepto", 0.6)
    if "poder" in lower:
        return ClassificationResult("poder_especial", "general", "poder especial", 0.7)
    if "arrendamiento" in lower or "arriendo" in lower:
        return ClassificationResult("contrato_arrendamiento", "civil", "arrendamiento", 0.7)
    if "contrato" in lower:
        return ClassificationResult("contrato_prestacion_servicios", "comercial", "prestación servicios", 0.6)
    return ClassificationResult("tutela", "general", "general", 0.4)
