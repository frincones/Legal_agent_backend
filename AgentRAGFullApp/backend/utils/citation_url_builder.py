"""Sprint M17 · Construye URLs canónicas a fuentes oficiales colombianas.

Para cada `ParsedCitation`, intenta construir una URL determinística que
apunte al texto oficial. Si no se puede construir, retorna None y el
EvidenceAccumulator cae a web search restringido a dominios oficiales.

Patrones validados manualmente con curl HEAD requests:

  Constitución      → corteconstitucional.gov.co/inicio/Constitucion%20politica%20de%20Colombia.pdf
  Códigos           → secretariasenado.gov.co/senado/basedoc/{codigo_slug}.html#{art}
  Leyes             → secretariasenado.gov.co/senado/basedoc/ley_{NNNN}_{YYYY}.html
  Decretos          → suin-juriscol.gov.co/viewDocument.asp?ruta=Decretos/{N}_{Y}
  Corte CC (T/C/SU)→ corteconstitucional.gov.co/relatoria/{YYYY}/{TIPO}-{N}-{YY}.htm
  CSJ Laboral SU- → cortesuprema.gov.co/corte/index.php/search/{TIPO}{N}/
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


# Slugs de codigos en Senado SUIN (validados con curl)
_CODIGO_URL_SLUG = {
    "CST":          "codigo_sustantivo_trabajo",
    "C.P.":         "codigo_penal",
    "C.C.":         "codigo_civil",
    "C.CO.":        "codigo_comercio",
    "CGP":          "ley_1564_2012",            # CGP es Ley 1564/2012
    "CPACA":        "ley_1437_2011",            # CPACA es Ley 1437/2011
    "CPTSS":        "ley_712_2001",             # alias
    "CPP":          "ley_906_2004",             # CPP es Ley 906/2004
}


def build_canonical_url(parsed) -> Optional[str]:
    """Construye URL canónica a fuente oficial.

    Retorna None solo si el kind/tipo no tiene patrón conocido —
    en cuyo caso el caller debe invocar fallback web search.

    parsed: ParsedCitation (de utils.citation_verifier)
    """
    if parsed is None:
        return None

    kind = parsed.kind
    tipo = parsed.tipo
    numero = parsed.numero
    anio = parsed.anio

    # ── CONSTITUCION ──
    if (kind in ("codigo", "codigo_articulo")) and tipo == "CONSTITUCION":
        # PDF oficial Corte Constitucional
        return "https://www.corteconstitucional.gov.co/inicio/Constitucion%20politica%20de%20Colombia.pdf"

    # ── CÓDIGOS (CST, C.P., C.C., C.CO., CGP, CPACA, CPP) ──
    if kind == "codigo" and tipo in _CODIGO_URL_SLUG:
        slug = _CODIGO_URL_SLUG[tipo]
        return f"https://www.secretariasenado.gov.co/senado/basedoc/{slug}.html"

    # ── CÓDIGO + ARTÍCULO ──
    if kind == "codigo_articulo" and tipo in _CODIGO_URL_SLUG:
        slug = _CODIGO_URL_SLUG[tipo]
        # Senado usa #N como ancla del artículo
        return f"https://www.secretariasenado.gov.co/senado/basedoc/{slug}.html#{numero}"

    # ── LEY ──
    if kind == "ley" and numero and anio:
        # Senado format: ley_NNNN_YYYY.html (zero-padded a 4)
        return f"https://www.secretariasenado.gov.co/senado/basedoc/ley_{numero:04d}_{anio}.html"

    # ── DECRETO ──
    if kind == "decreto" and numero and anio:
        # SUIN-Juriscol patrón
        return f"https://www.suin-juriscol.gov.co/viewDocument.asp?ruta=Decretos/{numero}_{anio}"

    # ── JURISPRUDENCIA Corte Constitucional ──
    if kind == "jurisprudencia" and tipo in ("T", "C", "SU", "A") and numero and anio:
        yy = str(anio)[-2:]
        # SU usa formato sin guión entre prefix y número (M11 hallazgo)
        if tipo == "SU":
            return f"https://www.corteconstitucional.gov.co/relatoria/{anio}/{tipo}{numero}-{yy}.htm"
        # Padding según convención
        if numero < 1000:
            num_str = f"{numero:03d}"
        else:
            num_str = str(numero)
        return f"https://www.corteconstitucional.gov.co/relatoria/{anio}/{tipo}-{num_str}-{yy}.htm"

    # ── JURISPRUDENCIA CSJ (SL, SC, SP, STC, STL, STP) ──
    if kind == "jurisprudencia" and tipo in ("SL", "SC", "SP", "STC", "STL", "STP") and numero:
        # CSJ no tiene URL única por sentencia; usar search del WordPress
        return f"https://cortesuprema.gov.co/corte/index.php/search/{tipo}{numero}/"

    # ── JURISPRUDENCIA Consejo de Estado ──
    if kind == "jurisprudencia" and tipo == "CE" and numero and anio:
        # Consejo de Estado no tiene URL predecible; usar buscador oficial
        return f"https://www.consejodeestado.gov.co/buscador-jurisprudencial/?q=CE-{numero}-{anio}"

    logger.debug("build_canonical_url: no pattern for kind=%s tipo=%s", kind, tipo)
    return None


def build_search_fallback_url(parsed) -> str:
    """Último recurso: link a búsqueda en sitios oficiales.

    Garantizado siempre retorna una URL (nunca None) usando Google CSE
    restringido a dominios oficiales gov.co.
    """
    query = parsed.normalized if parsed else "norma colombiana"
    # Google search restringido a dominios oficiales
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
    """Si la norma fue derogada por otra, construye URL a la vigente.

    derogada_por: ref de la norma que reemplaza (ej. "Ley 2010/2019")
    """
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
    """Garantiza que SIEMPRE haya una URL.

    Cascade:
      1. URL existente si presente
      2. URL canónica construida
      3. URL de búsqueda fallback (Google CSE restringido)

    Retorna SIEMPRE un string (nunca None) — esto cumple el requisito
    del usuario: "imposible siempre debe existir la fuente de origen".
    """
    if existing_url:
        return existing_url
    canonical = build_canonical_url(parsed)
    if canonical:
        return canonical
    return build_search_fallback_url(parsed)
