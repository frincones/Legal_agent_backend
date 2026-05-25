"""Sprint M17 · Construye URLs canónicas a fuentes oficiales colombianas.

Para cada `ParsedCitation`, genera una LISTA priorizada de URLs candidatas
(no una sola). El verifier hace HEAD a cada candidata hasta encontrar una
que responda HTTP 200, garantizando que la URL final SIEMPRE funciona
para el usuario.

Patrones validados manualmente con curl (mayo 2026):

  Constitución      → funcionpublica.gov.co/eva/gestornormativo/norma.php?i=4125
                     (alt: suin-juriscol.gov.co/viewDocument.asp?id=1687988)
  Códigos           → funcionpublica.gov.co/eva/gestornormativo/norma.php?i={id}
                     (alt: icbf.gov.co/cargues/avance/docs/{slug}.html)
  Leyes             → secretariasenado.gov.co/senado/basedoc/ley_{NNNN}_{YYYY}.html
                     (alt: funcionpublica.gov.co + suin-juriscol)
  Decretos          → suin-juriscol.gov.co/viewDocument.asp?ruta=Decretos/{N}_{Y}
                     (alt: funcionpublica.gov.co)
  Corte CC (T/C/SU)→ corteconstitucional.gov.co/relatoria/{YYYY}/{TIPO}-{N}-{YY}.htm
  CSJ Laboral SL/SC → cortesuprema.gov.co/corte/index.php/search/{TIPO}{N}/
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


# IDs en Función Pública (DAFP) → norma.php?i={id}
# VALIDADOS con GET + body inspection mayo 2026 (size>20KB y sin "norma_error.php"):
#   CONSTITUCION i=4125   → 1.2MB   OK
#   C.P.         i=6388   → 605KB   OK
#   C.CO.        i=41102  → 1.2MB   OK
#   CGP          i=48425  → 1MB     OK
#   CPACA        i=41249  → 622KB   OK
#   CPP          i=14787  → 613KB   OK
# CST y C.C. NO existen en Función Pública (todos los IDs probados → soft 404).
_FP_NORMA_ID = {
    "CONSTITUCION": 4125,
    "C.P.":         6388,
    "C.CO.":        41102,
    "CGP":          48425,
    "CPACA":        41249,
    "CPP":          14787,
}

# IDs Función Pública para Leyes comunes (validados GET + body inspection).
# Cuando "Ley N/YYYY" coincide aquí, se prefiere FP sobre Senado (server lento).
_FP_LEY_ID: dict[tuple[int, int], int] = {
    (50, 1990):   281,
    (100, 1993):  5248,
    (361, 1997):  343,
    (712, 2001):  4486,    # CPTSS (verificar)
    (789, 2002):  6778,
    (1010, 2006): 18843,
    (1437, 2011): 41249,   # CPACA
    (1564, 2012): 48425,   # CGP
}

# Slugs ICBF (mirror oficial gov.co) — fuente primary para CST y C.C.
# Validados con GET (>117KB, contenido real)
_CODIGO_ICBF_SLUG = {
    "CST":          "codigo_sustantivo_trabajo_pr001",
    "C.C.":         "codigo_civil",
}

# Slugs secretariasenado.gov.co (fallback — server intermitente / timeouts)
_CODIGO_URL_SLUG = {
    "CST":          "codigo_sustantivo_trabajo",
    "C.P.":         "codigo_penal",
    "C.C.":         "codigo_civil",
    "C.CO.":        "codigo_comercio",
    "CGP":          "ley_1564_2012",
    "CPACA":        "ley_1437_2011",
    "CPTSS":        "ley_712_2001",
    "CPP":          "ley_906_2004",
}


def build_url_candidates(parsed) -> list[str]:
    """Genera lista de URLs candidatas ORDENADAS por probabilidad de éxito.

    El verifier hace HEAD a cada una hasta encontrar HTTP 200.
    Esto garantiza que la URL mostrada al usuario SIEMPRE responda.
    """
    if parsed is None:
        return []

    kind = parsed.kind
    tipo = parsed.tipo
    numero = parsed.numero
    anio = parsed.anio
    candidates: list[str] = []

    # ── CONSTITUCION ──
    if (kind in ("codigo", "codigo_articulo")) and tipo == "CONSTITUCION":
        fp_id = _FP_NORMA_ID["CONSTITUCION"]
        anchor = f"#{numero}" if (kind == "codigo_articulo" and numero) else ""
        # Primary: Función Pública (más estable)
        candidates.append(
            f"https://www.funcionpublica.gov.co/eva/gestornormativo/norma.php?i={fp_id}{anchor}"
        )
        # Alt 1: SUIN-Juriscol
        candidates.append(
            f"https://www.suin-juriscol.gov.co/viewDocument.asp?id=1687988{anchor}"
        )
        # Alt 2: Corte Constitucional (PDF puede 404)
        candidates.append(
            "https://www.corteconstitucional.gov.co/inicio/Constitucion%20politica%20de%20Colombia.pdf"
        )
        return candidates

    # ── CÓDIGOS (CST, C.P., C.C., C.CO., CGP, CPACA, CPP) ──
    if kind in ("codigo", "codigo_articulo") and tipo in (
        set(_FP_NORMA_ID) | set(_CODIGO_ICBF_SLUG) | set(_CODIGO_URL_SLUG)
    ):
        anchor = f"#{numero}" if (kind == "codigo_articulo" and numero) else ""
        # Primary: ICBF si está disponible (para CST/C.C. es la única opción real)
        if tipo in _CODIGO_ICBF_SLUG:
            slug = _CODIGO_ICBF_SLUG[tipo]
            candidates.append(
                f"https://www.icbf.gov.co/cargues/avance/docs/{slug}.html"
            )
        # Si está en Función Pública (códigos con ID validado), agregar también
        if tipo in _FP_NORMA_ID:
            fp_id = _FP_NORMA_ID[tipo]
            candidates.append(
                f"https://www.funcionpublica.gov.co/eva/gestornormativo/norma.php?i={fp_id}{anchor}"
            )
        # Senado (server intermitente, último recurso)
        if tipo in _CODIGO_URL_SLUG:
            slug = _CODIGO_URL_SLUG[tipo]
            candidates.append(
                f"https://www.secretariasenado.gov.co/senado/basedoc/{slug}.html{anchor}"
            )
        return candidates

    # ── LEY ──
    if kind == "ley" and numero and anio:
        # Primary: Función Pública si tenemos ID validado (URL directa con contenido real)
        fp_id = _FP_LEY_ID.get((numero, anio))
        if fp_id:
            candidates.append(
                f"https://www.funcionpublica.gov.co/eva/gestornormativo/norma.php?i={fp_id}"
            )
        # Alt 1: Senado (oficial; responde intermitente)
        candidates.append(
            f"https://www.secretariasenado.gov.co/senado/basedoc/ley_{numero:04d}_{anio}.html"
        )
        # Alt 2: Función Pública busqueda (siempre responde, no es URL directa)
        candidates.append(
            f"https://www.funcionpublica.gov.co/eva/gestornormativo/gestornormativo.php?action=fichaaa&search=Ley+{numero}+de+{anio}"
        )
        return candidates

    # ── DECRETO ──
    if kind == "decreto" and numero and anio:
        # Primary: SUIN-Juriscol patrón directo
        candidates.append(
            f"https://www.suin-juriscol.gov.co/viewDocument.asp?ruta=Decretos/{numero}_{anio}"
        )
        # Alt: Función Pública search
        candidates.append(
            f"https://www.funcionpublica.gov.co/eva/gestornormativo/gestornormativo.php?action=fichaaa&search=Decreto+{numero}+de+{anio}"
        )
        return candidates

    # ── JURISPRUDENCIA Corte Constitucional ──
    if kind == "jurisprudencia" and tipo in ("T", "C", "SU", "A") and numero and anio:
        yy = str(anio)[-2:]
        # SU usa formato sin guión entre prefix y número
        if tipo == "SU":
            candidates.append(
                f"https://www.corteconstitucional.gov.co/relatoria/{anio}/{tipo}{numero}-{yy}.htm"
            )
        else:
            num_str = f"{numero:03d}" if numero < 1000 else str(numero)
            candidates.append(
                f"https://www.corteconstitucional.gov.co/relatoria/{anio}/{tipo}-{num_str}-{yy}.htm"
            )
        # Alt: search en relatoría
        candidates.append(
            f"https://www.corteconstitucional.gov.co/buscar?q={tipo}-{numero}-{yy}"
        )
        return candidates

    # ── JURISPRUDENCIA CSJ (SL, SC, SP, STC, STL, STP) ──
    if kind == "jurisprudencia" and tipo in ("SL", "SC", "SP", "STC", "STL", "STP") and numero:
        # Primary: search en WordPress CSJ
        candidates.append(
            f"https://cortesuprema.gov.co/corte/index.php/search/{tipo}{numero}/"
        )
        # Alt: relatoría CSJ
        candidates.append(
            f"https://relatoria.cortesuprema.gov.co/index.php?action=search&query={tipo}{numero}"
        )
        return candidates

    # ── JURISPRUDENCIA Consejo de Estado ──
    if kind == "jurisprudencia" and tipo == "CE" and numero and anio:
        candidates.append(
            f"https://www.consejodeestado.gov.co/buscador-jurisprudencial/?q=CE-{numero}-{anio}"
        )
        return candidates

    logger.debug("build_url_candidates: no pattern for kind=%s tipo=%s", kind, tipo)
    return []


def build_canonical_url(parsed) -> Optional[str]:
    """Wrapper retro-compatible: retorna el primer candidato (más probable).

    NOTA: usar `build_url_candidates` para HEAD-validation cascade en
    `verification_agent`. Esta función queda para compat con código viejo.
    """
    candidates = build_url_candidates(parsed)
    return candidates[0] if candidates else None


def build_search_fallback_url(parsed) -> str:
    """Último recurso: link a búsqueda en sitios oficiales.

    Garantizado siempre retorna una URL (nunca None) usando Google search
    restringido a dominios oficiales gov.co.
    """
    query = parsed.normalized if parsed else "norma colombiana"
    sites = " OR ".join([
        "site:secretariasenado.gov.co",
        "site:corteconstitucional.gov.co",
        "site:cortesuprema.gov.co",
        "site:suin-juriscol.gov.co",
        "site:funcionpublica.gov.co",
    ])
    from urllib.parse import quote_plus
    full_query = f'"{query}" ({sites})'
    return f"https://www.google.com/search?q={quote_plus(full_query)}"


def build_derogada_url(derogada_por: Optional[str]) -> Optional[str]:
    """Si la norma fue derogada por otra, construye URL a la vigente."""
    if not derogada_por:
        return None
    try:
        from utils.citation_verifier import parse_citation_ref
        parsed = parse_citation_ref(derogada_por)
        if parsed:
            return build_canonical_url(parsed)
    except Exception:
        pass
    return None


def guarantee_fuente_url(parsed, existing_url: Optional[str] = None) -> str:
    """Garantiza que SIEMPRE haya una URL (sin HEAD validation).

    Cascade:
      1. URL existente
      2. Primer candidato canónico
      3. Búsqueda fallback gov.co

    Para validación HEAD usar `validate_and_pick_best_url` en
    `utils.url_validator`.
    """
    if existing_url:
        return existing_url
    canonical = build_canonical_url(parsed)
    if canonical:
        return canonical
    return build_search_fallback_url(parsed)
