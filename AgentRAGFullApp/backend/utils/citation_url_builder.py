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


# ID en Función Pública (DAFP) → URL más estable que secretariasenado
# Validados con curl HEAD HTTP 200 mayo 2026
_FP_NORMA_ID = {
    "CONSTITUCION": 4125,
    "CST":          33104,
    "C.P.":         6388,    # Código Penal (Ley 599/2000)
    "C.C.":         39535,   # Código Civil
    "C.CO.":        14790,   # Código de Comercio (Decreto 410/1971)
    "CGP":          48425,   # Ley 1564/2012
    "CPACA":        41249,   # Ley 1437/2011
    "CPP":          14787,   # Ley 906/2004
}

# Slugs de codigos en secretariasenado (fallback, server intermitente)
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

# Slugs ICBF (mirror oficial gov.co alternativo)
_CODIGO_ICBF_SLUG = {
    "CST":          "codigo_sustantivo_trabajo_pr001",
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
    if kind in ("codigo", "codigo_articulo") and tipo in _CODIGO_URL_SLUG:
        anchor = f"#{numero}" if (kind == "codigo_articulo" and numero) else ""
        # Primary: Función Pública (estable)
        if tipo in _FP_NORMA_ID:
            fp_id = _FP_NORMA_ID[tipo]
            candidates.append(
                f"https://www.funcionpublica.gov.co/eva/gestornormativo/norma.php?i={fp_id}{anchor}"
            )
        # Alt 1: ICBF mirror (algunos códigos)
        if tipo in _CODIGO_ICBF_SLUG:
            slug = _CODIGO_ICBF_SLUG[tipo]
            candidates.append(
                f"https://www.icbf.gov.co/cargues/avance/docs/{slug}.html"
            )
        # Alt 2: Senado (server intermitente, último recurso)
        slug = _CODIGO_URL_SLUG[tipo]
        candidates.append(
            f"https://www.secretariasenado.gov.co/senado/basedoc/{slug}.html{anchor}"
        )
        return candidates

    # ── LEY ──
    if kind == "ley" and numero and anio:
        # Primary: Senado (cuando responde)
        candidates.append(
            f"https://www.secretariasenado.gov.co/senado/basedoc/ley_{numero:04d}_{anio}.html"
        )
        # Alt: Función Pública search
        candidates.append(
            f"https://www.funcionpublica.gov.co/eva/gestornormativo/gestornormativo.php?action=fichaaa&search=Ley+{numero}+de+{anio}"
        )
        # Alt: SUIN-Juriscol search
        candidates.append(
            f"https://www.suin-juriscol.gov.co/index.html?q=Ley+{numero}+{anio}"
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
