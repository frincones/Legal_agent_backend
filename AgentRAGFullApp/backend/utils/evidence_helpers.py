"""Sprint 21 · Evidence helpers.

  · hash_document_text          · hash determinístico para cache
  · classify_doc_features        · características del doc (notariado, fecha cierta, etc.)
  · score_level                  · convierte score 0-100 a nivel
  · summarize_validation         · convierte resultado de identity_providers a algo legible
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional


def hash_document_text(text: str) -> str:
    """Hash determinístico del texto · usado para cache de análisis LLM."""
    h = hashlib.sha256()
    h.update((text or "").strip().encode("utf-8", errors="ignore"))
    return h.hexdigest()[:32]


# --------------------------------------------------------------------
# Feature classifier · heurísticas simples sobre el texto del doc
# --------------------------------------------------------------------
_NOTARIAL_KEYWORDS = (
    "notaría", "notaria", "escritura pública", "notario público",
    "fe pública", "protocolizado", "registro instrumento",
)
_OFFICIAL_STAMP_KEYWORDS = (
    "sello oficial", "sello seco", "estampilla", "se firma", "ante mí",
)
_SIGNED_KEYWORDS = (
    "firmado", "suscrito", "cédula de ciudadanía", "c.c.", "firma digital",
    "firma electrónica", "firma autógrafa", "rubricado",
)
_OFFICIAL_FORMAT_KEYWORDS = (
    "ministerio", "superintendencia", "rama judicial", "dian",
    "registraduría", "icbf", "fiscalía",
)


def classify_doc_features(text: str, mime_type: Optional[str] = None,
                          metadata: Optional[dict] = None) -> dict:
    """Heurística simple · NO es exhaustiva ni reemplaza criterio humano.
    Devuelve banderas booleanas que el scorer combina."""
    t = (text or "").lower()
    metadata = metadata or {}

    has_notarial = any(k in t for k in _NOTARIAL_KEYWORDS)
    has_official_stamp = any(k in t for k in _OFFICIAL_STAMP_KEYWORDS)
    has_signature_mention = any(k in t for k in _SIGNED_KEYWORDS)
    is_official_source = any(k in t for k in _OFFICIAL_FORMAT_KEYWORDS)

    # ¿es PDF? mejor que docx para evidencia (menos editable)
    is_pdf = bool(mime_type and "pdf" in mime_type.lower())

    # ¿tiene fecha clara? buscar "AAAA" o "DD de MM"
    has_date_reference = bool(
        re.search(r"\b(19|20)\d{2}\b", t) and
        re.search(r"\b(enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
                  r"septiembre|octubre|noviembre|diciembre)\b", t)
    )

    # ¿tiene número de radicado / consecutivo?
    has_radicado = bool(re.search(r"\b(radicad[oa]|consecutivo)\s*[:#]?\s*[A-Z0-9\-]{4,}", t))

    # ¿menciona normatividad? (referencia a ley/decreto/sentencia)
    has_legal_refs = bool(
        re.search(r"\b(ley|decreto|resolución|sentencia|art[íi]culo)\s+\d", t)
    )

    return {
        "is_pdf": is_pdf,
        "has_notarial": has_notarial,
        "has_official_stamp": has_official_stamp,
        "has_signature_mention": has_signature_mention,
        "is_official_source": is_official_source,
        "has_date_reference": has_date_reference,
        "has_radicado": has_radicado,
        "has_legal_refs": has_legal_refs,
        "char_length": len(t),
    }


# --------------------------------------------------------------------
# Score → level mapping
# --------------------------------------------------------------------
def score_level(score: int) -> str:
    """Convierte score 0-100 a nivel cualitativo."""
    if score >= 80:
        return "fuerte"
    if score >= 60:
        return "medio"
    if score >= 35:
        return "debil"
    return "cuestionable"


def summarize_validation_result(result: dict) -> str:
    """Convierte el resultado de un provider en una línea legible."""
    provider = result.get("provider", "?")
    status = result.get("status", "?")
    if status == "matched":
        return f"{provider}: ✓ datos coinciden"
    if status == "mismatch":
        return f"{provider}: ⚠ nombre no coincide con registro oficial"
    if status == "partial":
        conf = (result.get("payload") or {}).get("match_confidence", 0)
        return f"{provider}: ~ coincidencia parcial ({int(conf * 100)}%)"
    if status == "not_found":
        return f"{provider}: ✗ no encontrado en registros"
    if status == "not_configured":
        return f"{provider}: (proveedor no configurado · usando mock)"
    if status == "error":
        return f"{provider}: error · {result.get('error', 'sin detalle')}"
    return f"{provider}: {status}"
