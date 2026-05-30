"""Sprint M20.07 · S7.3 · Opus 4.7 selector refinado con complexity_score.

Hoy: lista OPUS_DOC_TYPES estática (5 doc_types).
Refinado: combina doc_type + features dinámicas (longitud intent, # citas
detectadas, matter complejo) para decidir.

Beneficio: Opus solo cuando aporta valor real (concepto_juridico breve
podría seguir con Sonnet; demanda_civil compleja con muchas pruebas
podría escalar a Opus).
"""
from __future__ import annotations

import re
from typing import Optional


# Threshold default (configurable env var en producción)
DEFAULT_OPUS_THRESHOLD = 0.65


# Doc types que SIEMPRE escalan a Opus (independiente del score)
ALWAYS_OPUS_DOC_TYPES = {
    "concepto_juridico",
    "casacion",
    "demanda_compleja",
}


# Doc types que NUNCA escalan a Opus (overkill)
NEVER_OPUS_DOC_TYPES = {
    "poder_especial",
    "revocatoria_poder",
    "derecho_peticion",
}


_CITATION_PATTERN = re.compile(
    r"\b(?:art(?:s|ículos)?\.?\s*\d+|ley\s+\d+|decreto\s+\d+|"
    r"sentencia\s+[A-Z]+-?\d+|T-\d+|C-\d+|SU-\d+|SC\d+)\b",
    re.IGNORECASE,
)


def compute_complexity_score(
    doc_type: str,
    intent: str,
    brief: str = "",
    matter_metadata: Optional[dict] = None,
) -> float:
    """Score 0.0-1.0 indicando complejidad del request.

    Features:
      - longitud combinada intent + brief (más texto → más complejo)
      - número de citas legales detectadas en el texto
      - matter context: si hay matter activo con N documentos previos
      - keywords de complejidad: "casación", "amplio", "compleja", "tutela colectiva", "abusiva"
    """
    text = (intent or "") + " " + (brief or "")
    text_len = len(text)

    # Length feature (0-0.3)
    if text_len < 200:
        length_score = 0.0
    elif text_len < 500:
        length_score = 0.1
    elif text_len < 1000:
        length_score = 0.2
    else:
        length_score = 0.3

    # Citations feature (0-0.3)
    n_citations = len(_CITATION_PATTERN.findall(text))
    citations_score = min(0.3, n_citations * 0.05)

    # Matter context feature (0-0.2)
    matter_score = 0.0
    if matter_metadata:
        n_docs = matter_metadata.get("documents_count", 0)
        if n_docs >= 5:
            matter_score = 0.2
        elif n_docs >= 2:
            matter_score = 0.1

    # Keywords feature (0-0.2)
    keywords_score = 0.0
    keyword_hits = [
        kw for kw in (
            "casación", "casacion", "amplio", "compleja", "complejo",
            "abusiva", "abusivo", "colectiva", "multidisciplinaria",
            "fuero", "sindical", "tutela contra providencia",
        )
        if kw.lower() in text.lower()
    ]
    keywords_score = min(0.2, len(keyword_hits) * 0.05)

    total = length_score + citations_score + matter_score + keywords_score
    return min(1.0, total)


def should_use_opus(
    doc_type: str,
    intent: str,
    brief: str = "",
    matter_metadata: Optional[dict] = None,
    threshold: float = DEFAULT_OPUS_THRESHOLD,
) -> tuple[bool, dict]:
    """Retorna (use_opus, decision_breakdown).

    Lógica:
      1. Si doc_type en ALWAYS_OPUS_DOC_TYPES → True
      2. Si doc_type en NEVER_OPUS_DOC_TYPES → False
      3. Si complexity_score >= threshold → True
      4. Sino → False (Sonnet)
    """
    breakdown = {
        "doc_type": doc_type,
        "rule": None,
        "complexity_score": None,
        "threshold": threshold,
        "use_opus": False,
    }

    if doc_type in ALWAYS_OPUS_DOC_TYPES:
        breakdown["rule"] = "always_opus_doc_type"
        breakdown["use_opus"] = True
        return True, breakdown

    if doc_type in NEVER_OPUS_DOC_TYPES:
        breakdown["rule"] = "never_opus_doc_type"
        return False, breakdown

    score = compute_complexity_score(doc_type, intent, brief, matter_metadata)
    breakdown["complexity_score"] = score
    breakdown["rule"] = "complexity_threshold"
    breakdown["use_opus"] = score >= threshold
    return breakdown["use_opus"], breakdown
