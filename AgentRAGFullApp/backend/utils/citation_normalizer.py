"""Sprint M13 · CanonicalAliasExpander para citas legales colombianas.

Convierte texto libre del LLM ("Art. 64 Código Sustantivo del Trabajo") a
formato canónico que parse_citation_ref() pueda procesar ("Art. 64 CST").

PRIORIDAD CRÍTICA: jurisprudencia debe parsearse ANTES que códigos para
evitar que "C-1507/2000" se confunda con "Constitución Política" (la C de
sentencia C-XXX/AAAA es la misma C de Constitución).

Llamado desde:
  - utils/citation_verifier.py:parse_citation_ref() (pre-procesado)
  - lex/verify/citation_verifier.py:_normalize_for_legacy() (reuso)
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional


# ────────────────────────────────────────────────────────────────────
# Aliases extendidos — nombres completos → forma corta canónica
# ────────────────────────────────────────────────────────────────────

# IMPORTANTE: orden importa. Constitución antes que CN (alias corto).
# Patrones case-insensitive, acentos opcionales.
_EXTENDED_ALIASES: list[tuple[re.Pattern, str]] = [
    # Constitución
    (re.compile(r"\bconstituci[oó]n\s+pol[ií]tica(?:\s+de\s+colombia)?(?:\s+de\s+1991)?\b", re.IGNORECASE), "CONSTITUCION"),
    (re.compile(r"\bconstituci[oó]n\s+de\s+1991\b", re.IGNORECASE), "CONSTITUCION"),
    (re.compile(r"\bconstituci[oó]n\b", re.IGNORECASE), "CONSTITUCION"),
    (re.compile(r"\bC\.\s*N\.\s*(?:91)?\b"), "CONSTITUCION"),
    (re.compile(r"\bCN\s*91\b"), "CONSTITUCION"),
    (re.compile(r"\bCN/91\b"), "CONSTITUCION"),

    # Códigos sustantivos
    (re.compile(r"\bc[oó]digo\s+sustantivo\s+(?:del\s+)?trabajo\b", re.IGNORECASE), "CST"),
    (re.compile(r"\bC\.\s*S\.\s*T\.\b"), "CST"),

    # Código General del Proceso
    (re.compile(r"\bc[oó]digo\s+general\s+del\s+proceso\b", re.IGNORECASE), "CGP"),

    # Códigos procesales
    (re.compile(r"\bc[oó]digo\s+procesal\s+(?:del\s+)?trabajo\b", re.IGNORECASE), "CPTSS"),
    (re.compile(r"\bc[oó]digo\s+(?:de\s+)?procedimiento\s+penal\b", re.IGNORECASE), "CPP"),
    (re.compile(r"\bc[oó]digo\s+(?:de\s+)?procedimiento\s+civil\b", re.IGNORECASE), "CPC"),
    (re.compile(r"\bc[oó]digo\s+(?:de\s+)?procedimiento\s+administrativo\b", re.IGNORECASE), "CPACA"),
    (re.compile(r"\bCPACA\b"), "CPACA"),

    # Comercio (antes que Civil para no matchear "Co" en "Comercio" como C.C.)
    (re.compile(r"\bc[oó]digo\s+(?:de\s+)?comercio\b", re.IGNORECASE), "C.CO."),
    (re.compile(r"\bC\.\s*Co\.\b"), "C.CO."),
    (re.compile(r"\bCCom\b"), "C.CO."),

    # Civil
    (re.compile(r"\bc[oó]digo\s+civil\b", re.IGNORECASE), "C.C."),

    # Penal (incluye "CP" sin puntos — alias técnico oficial)
    (re.compile(r"\bc[oó]digo\s+penal\b", re.IGNORECASE), "C.P."),
    (re.compile(r"\bC\.\s*P\.\b"), "C.P."),
    # M16: 'CN' sin puntos como Constitución (uso común del LLM)
    (re.compile(r"(?<![A-Z])CN(?![A-Z0-9])"), "CONSTITUCION"),

    # Otros códigos
    (re.compile(r"\bestatuto\s+tributario\b", re.IGNORECASE), "ET"),
    (re.compile(r"\bc[oó]digo\s+de\s+la\s+infancia\s+y\s+adolescencia\b", re.IGNORECASE), "CIA"),
    (re.compile(r"\bc[oó]digo\s+sustantivo\s+(?:de\s+)?(?:la\s+)?seguridad\s+social\b", re.IGNORECASE), "CSS"),
]


# Patrones de detección de jurisprudencia. Si matchean, NO se aplica expander
# de códigos (evita confundir C-1507/2000 con Constitución).
_JURISP_GUARD = re.compile(
    r"\b(?:STC|STL|STP|SU|SC|SL|SP|CE)[\s\-]*\d{1,5}[\s/\-]+(?:de\s+)?\d{2,4}\b"
    r"|\b[CTAS][U]?\s*-?\s*\d{1,5}[/-]\d{2,4}\b",
    re.IGNORECASE
)


def _strip_accents(text: str) -> str:
    """Quita acentos para matching robusto."""
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


def _collapse_whitespace(text: str) -> str:
    """Colapsa múltiples espacios/tabs/newlines a uno solo."""
    return re.sub(r"\s+", " ", text).strip()


def expand_to_canonical(raw: str) -> tuple[str, bool]:
    """Convierte texto libre a forma canónica para parse_citation_ref().

    Returns:
        (canonical_text, was_expanded)

    Ejemplos:
        "Art. 64 Código Sustantivo del Trabajo" → ("Art. 64 CST", True)
        "Artículo 25 de la Constitución Política de 1991" → ("Art. 25 CONSTITUCION", True)
        "T-760/2008" → ("T-760/2008", False)  ← jurisprudencia, no expandir
        "C-1507/2000" → ("C-1507/2000", False)  ← jurisprudencia C, NO Constitución
        "LEY 50/1990" → ("LEY 50/1990", False)  ← ya parseable
    """
    if not raw:
        return raw, False

    text = _collapse_whitespace(raw)

    # GUARD CRÍTICO: si parece jurisprudencia (SU-XXX/AAAA, C-XXX/AAAA, etc.),
    # NO aplicar expander de códigos. La C de "C-1507/2000" no es Constitución.
    if _JURISP_GUARD.search(text):
        return text, False

    # Aplicar aliases en orden (primero match gana, pero re.sub reemplaza TODOS)
    was_expanded = False
    for pattern, replacement in _EXTENDED_ALIASES:
        new_text = pattern.sub(replacement, text)
        if new_text != text:
            was_expanded = True
            text = new_text

    if was_expanded:
        text = _collapse_whitespace(text)

    return text, was_expanded


def detect_articulo(raw: str) -> Optional[int]:
    """Extrae el número de artículo si está presente.

    "Art. 64 CST" → 64
    "Artículo 25 de la Constitución" → 25
    "art 99 ley 50/90" → 99
    "Ley 50/1990" → None (es ley, no artículo)
    """
    m = re.search(r"art(?:[íi]culo|\.|\s)\s*(\d+)", raw, re.IGNORECASE)
    return int(m.group(1)) if m else None


def detect_codigo(raw: str) -> Optional[str]:
    """Detecta si el texto se refiere a un código legal.

    Returns el alias canónico (CST, CGP, etc.) o None.
    """
    if _JURISP_GUARD.search(raw):
        return None
    for pattern, alias in _EXTENDED_ALIASES:
        if pattern.search(raw):
            return alias
    # Aliases cortos directos (incluyendo formas con puntos)
    upper = raw.upper()
    # Orden: más largos primero (CONSTITUCION antes que CST, CPACA antes que CP)
    short_aliases = [
        "CONSTITUCION", "CPACA", "CPTSS", "CST", "CGP", "CPP", "CPC",
        "ET", "CIA", "CSS",
        "C.CO.", "C.C.", "C.P.",
        # M16: aliases sin puntos comunes en redacción del LLM
        "CCO", "CC", "CP", "CN",
    ]
    for short in short_aliases:
        pattern = re.escape(short)
        # No usar \b porque puntos no cuentan como word boundary
        if re.search(rf"(?:^|[^A-Z0-9])({pattern})(?:[^A-Z0-9]|$)", upper):
            # Normalizar variantes sin puntos a versión con puntos del legacy
            if short == "CCO":
                return "C.CO."
            if short == "CC":
                return "C.C."
            if short == "CP":
                return "C.P."
            if short == "CN":
                return "CONSTITUCION"
            return short
    return None
