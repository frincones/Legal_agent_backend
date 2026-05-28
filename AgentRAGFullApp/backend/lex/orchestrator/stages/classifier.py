"""Stage 1: Classifier — detecta doc_type + jurisdicción + materia desde intent.

Usa gpt-4o-mini con JSON output (rápido + barato).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Catálogo soportado (alineado con lex/templates/contracts.py)
# M19.21.B: agregados nulidad/admin, pertenencia, responsabilidad civil, divorcio
SUPPORTED_DOC_TYPES = [
    "demanda_laboral_ordinaria",
    "demanda_civil_ordinaria",
    "demanda_ejecutivo_singular",
    "demanda_pertenencia",
    "demanda_responsabilidad_civil",
    "demanda_alimentos",
    "demanda_divorcio",
    "demanda_nulidad_restablecimiento",
    "demanda_simple_nulidad",
    "demanda_reparacion_directa",
    "tutela",
    "derecho_peticion",
    "accion_popular",
    "accion_grupo",
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
- demanda_laboral_ordinaria (cualquier demanda ante juez laboral: despido, prestaciones, acoso, fuero, contrato realidad)
- demanda_civil_ordinaria (procesos civiles declarativos generales)
- demanda_ejecutivo_singular (cobro de título ejecutivo: pagaré, factura, sentencia)
- demanda_pertenencia (acción de pertenencia / usucapión / prescripción adquisitiva)
- demanda_responsabilidad_civil (responsabilidad civil extracontractual, accidentes, daños)
- demanda_alimentos (fijación, aumento o disminución de cuota alimentaria)
- demanda_divorcio (divorcio contencioso, separación de cuerpos)
- demanda_nulidad_restablecimiento (CONTRA acto administrativo de entidad pública: DIAN, SIC, alcaldías, ministerios, superintendencias — pide nulidad + restablecimiento del derecho. Tribunal/Juez Administrativo)
- demanda_simple_nulidad (nulidad de acto administrativo de interés general, sin pretensión de restablecimiento)
- demanda_reparacion_directa (daño antijurídico causado por el Estado, sin acto administrativo)
- tutela (acción de tutela Art. 86 CN — derechos fundamentales)
- accion_popular (Ley 472/1998 — derechos colectivos)
- accion_grupo (Ley 472/1998 — daños masivos)
- derecho_peticion
- contrato_arrendamiento
- contrato_prestacion_servicios
- denuncia_penal
- recurso_apelacion
- concepto_juridico
- poder_especial

Jurisdicciones: laboral, civil, penal, constitucional, administrativo, familia, tributario, comercial, general.

REGLAS DE CLASIFICACIÓN IMPORTANTES (M19.21.B):
- Si menciona "DIAN", "SIC", "Superintendencia", "Alcaldía", "Ministerio", "Resolución sancionatoria",
  "CPACA", "Tribunal Administrativo", "Juez Administrativo", "Consejo de Estado" → es administrativo.
  Si pide DECLARAR NULIDAD y RESTABLECER → "demanda_nulidad_restablecimiento".
  Si solo pide nulidad sin restablecimiento → "demanda_simple_nulidad".
  Si pide reparación por daño del Estado sin acto administrativo → "demanda_reparacion_directa".
- Si menciona "prescripción adquisitiva", "usucapión", "posesión material" → "demanda_pertenencia".
- Si menciona "daños y perjuicios", "responsabilidad extracontractual", "accidente", "indemnización civil" → "demanda_responsabilidad_civil".
- Si menciona "divorcio", "separación de cuerpos" → "demanda_divorcio".
- Si menciona "tutela", "amparo", "derecho fundamental" → "tutela".
- Si menciona "alimentos", "cuota alimentaria" → "demanda_alimentos".
- Si tiene dudas entre civil ordinaria y un tipo específico, prefiere el específico.
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
    """Heurística simple si gpt-4o-mini falla. M19.21.B: agregadas reglas admin/familia/pertenencia."""
    lower = (intent or "").lower()
    # ADMINISTRATIVO — prioridad alta porque palabras como "demanda" son ambiguas
    if any(k in lower for k in [
        "nulidad y restablecimiento", "nulidad restablecimiento",
        "contra la dian", "contra dian", "contra la sic", "contra la superintendencia",
        "contra el ministerio", "contra la alcaldía", "tribunal administrativo",
        "juez administrativo", "consejo de estado", "cpaca", "ley 1437",
        "resolución sancionatoria", "acto administrativo",
    ]):
        return ClassificationResult(
            "demanda_nulidad_restablecimiento",
            "administrativo",
            "nulidad de acto administrativo + restablecimiento del derecho",
            0.85,
        )
    if "reparación directa" in lower or "reparacion directa" in lower or "daño antijurídico" in lower:
        return ClassificationResult("demanda_reparacion_directa", "administrativo", "reparación directa", 0.85)
    if "simple nulidad" in lower or "nulidad simple" in lower:
        return ClassificationResult("demanda_simple_nulidad", "administrativo", "simple nulidad", 0.8)
    # CONSTITUCIONAL
    if "tutela" in lower or "amparo" in lower:
        return ClassificationResult("tutela", "constitucional", "derechos fundamentales", 0.85)
    if "acción popular" in lower or "accion popular" in lower:
        return ClassificationResult("accion_popular", "constitucional", "derechos colectivos", 0.85)
    if "acción de grupo" in lower or "accion de grupo" in lower:
        return ClassificationResult("accion_grupo", "constitucional", "daños masivos", 0.85)
    # FAMILIA
    if "divorcio" in lower or "separación de cuerpos" in lower:
        return ClassificationResult("demanda_divorcio", "familia", "divorcio contencioso", 0.85)
    if "alimentos" in lower or "cuota alimentaria" in lower:
        return ClassificationResult("demanda_alimentos", "familia", "fijación cuota alimentaria", 0.8)
    # CIVIL específico
    if any(k in lower for k in ["pertenencia", "usucapión", "usucapion", "prescripción adquisitiva", "prescripcion adquisitiva"]):
        return ClassificationResult("demanda_pertenencia", "civil", "pertenencia / usucapión", 0.85)
    if any(k in lower for k in [
        "responsabilidad civil", "responsabilidad extracontractual",
        "daños y perjuicios", "accidente de tránsito", "accidente de transito",
        "indemnización civil", "lucro cesante", "daño emergente", "daño moral",
    ]):
        return ClassificationResult("demanda_responsabilidad_civil", "civil", "responsabilidad civil extracontractual", 0.8)
    if "ejecutivo" in lower or "pagaré" in lower or "pagare" in lower or "título ejecutivo" in lower:
        return ClassificationResult("demanda_ejecutivo_singular", "civil", "cobro ejecutivo", 0.7)
    # LABORAL
    if any(k in lower for k in ["laboral", "despido", "contrato realidad", "cesantías", "cesantias",
                                 "indemnización laboral", "fuero", "salario", "prestaciones sociales"]):
        return ClassificationResult("demanda_laboral_ordinaria", "laboral", "demanda laboral", 0.75)
    # OTROS
    if "petición" in lower or "peticion" in lower:
        return ClassificationResult("derecho_peticion", "administrativo", "petición", 0.7)
    if "denuncia" in lower or "querella" in lower:
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
    if "demanda" in lower:
        return ClassificationResult("demanda_civil_ordinaria", "civil", "general", 0.5)
    return ClassificationResult("tutela", "general", "general", 0.4)
