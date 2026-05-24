"""Sprint M10 · CSJ (Corte Suprema de Justicia) source — RSS feed search.

cortesuprema.gov.co NO tiene URL predictable por sentencia. Pero su buscador
WordPress expone los resultados como RSS feed:

  https://cortesuprema.gov.co/corte/index.php/search/{QUERY}/feed/rss2/

Si el RSS devuelve 0 <item>, la sentencia NO está indexada en la web oficial:
  - Posible alucinación del LLM
  - O sentencia muy reciente no indexada aún
  - O numeración alternativa que no coincide

Mucho más confiable que DuckDuckGo (que está bloqueado/CAPTCHA).
"""
from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger(__name__)

BASE = "https://cortesuprema.gov.co/corte/index.php/search"
TIMEOUT = httpx.Timeout(15.0, connect=8.0)


async def search_csj(citation_ref: str) -> Optional[dict]:
    """Busca una sentencia en CSJ via RSS feed.

    Returns:
        {"titulo": str, "fuente_url": str, "snippet": str} si encuentra al menos 1 item
        None si feed vacío (alta probabilidad de alucinación)
    """
    # Probar variantes del ref
    variants = _generate_variants(citation_ref)

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for q in variants:
            url = f"{BASE}/{quote_plus(q)}/feed/rss2/"
            try:
                resp = await client.get(url, headers={"User-Agent": "LexAI/1.0"})
                if resp.status_code != 200:
                    continue
                xml = resp.text
                # Parse simple: buscar <item>...</item>
                items = re.findall(r"<item>(.*?)</item>", xml, re.DOTALL)
                if not items:
                    continue
                # Si el RSS feed devuelve al menos 1 item, la cita aparece
                # mencionada en alguna página oficial de CSJ. Esto es suficiente
                # confirmación de que NO es alucinación pura (el ID existe en
                # el sitio oficial, aunque no necesariamente en una URL única).
                first = items[0]
                title_m = re.search(r"<title[^>]*>(.*?)</title>", first, re.DOTALL)
                link_m = re.search(r"<link[^>]*>(.*?)</link>", first, re.DOTALL)
                desc_m = re.search(r"<description[^>]*>(.*?)</description>", first, re.DOTALL)
                titulo = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1",
                                (title_m.group(1) if title_m else "")).strip()
                link = (link_m.group(1) if link_m else "").strip()
                desc = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1",
                              (desc_m.group(1) if desc_m else "")).strip()
                desc_clean = re.sub(r"<[^>]+>", "", desc)[:500]

                # Match exacto si haystack contiene:
                # - el ref completo normalizado: "sl14302022"
                # - O al menos el número específico + tipo: "sl1430"
                ref_norm = citation_ref.lower().replace(" ", "").replace("-", "").replace("/", "")
                haystack = (titulo + " " + link + " " + desc_clean).lower().replace(
                    " ", "").replace("-", "").replace("/", "")
                # Extraer SL+numero del ref
                short_match = re.search(r"(sl|sc|sp|stc|stl|stp)(\d{1,5})", ref_norm)
                short_ref = short_match.group(0) if short_match else None

                if ref_norm in haystack:
                    match_type = "exact"
                elif short_ref and short_ref in haystack:
                    # tipo+numero aparece (ej "sl1430" en algun analisis del CSJ)
                    # Es confirmación de que existe la sentencia
                    match_type = "exact"
                else:
                    match_type = "mention"

                logger.info("CSJ RSS %s match for %s: %s (%d items)",
                            match_type, q, link, len(items))
                return {
                    "titulo": titulo[:200],
                    "fuente_url": link,
                    "snippet": desc_clean,
                    "query_used": q,
                    "match_type": match_type,
                    "items_count": len(items),
                }
            except Exception as e:
                logger.warning("CSJ RSS fetch failed for %s: %s", q, e)
                continue

    return None


def _generate_variants(citation_ref: str) -> list[str]:
    """Variantes ortográficas para query CSJ.

    Priorizar variantes que sabemos producen hits buenos en CSJ WordPress search:
    - "SL1430" (solo número) → encuentra análisis de la sentencia
    - "SL 1430 de 2022" (con espacios + "de") → variante humana
    - "SL1430-2022" (formato canónico)
    """
    ref = citation_ref.strip().upper()
    variants = []

    # Detectar pattern SL/SC/SP/STC/STL/STP + numero + año
    m = re.match(r"(SL|SC|SP|STC|STL|STP)[\s\-]*(\d{1,5})[\s/\-]+(\d{2,4})", ref)
    if m:
        prefix, num, yr = m.group(1), m.group(2), m.group(3)
        # Año completo
        full_yr = yr if len(yr) == 4 else (f"20{yr}" if int(yr) < 50 else f"19{yr}")
        variants.extend([
            f"{prefix}{num}",                  # SL1430 (mejor hit observado)
            f"{prefix} {num} de {full_yr}",    # SL 1430 de 2022 (humano)
            f"{prefix}{num}-{full_yr}",        # SL1430-2022 (canónico)
            f"{prefix}-{num}-{full_yr}",       # SL-1430-2022
            f"{prefix} {num}",                 # SL 1430
            f"sentencia {prefix}{num}",        # sentencia SL1430
        ])
    variants.append(ref)  # ref original al final

    # Dedup
    seen = set()
    out = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out
