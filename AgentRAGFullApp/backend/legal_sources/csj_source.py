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

                # Determinar si es match exacto o solo mención
                ref_norm = citation_ref.lower().replace(" ", "").replace("-", "").replace("/", "")
                haystack = (titulo + " " + link + " " + desc_clean).lower().replace(
                    " ", "").replace("-", "").replace("/", "")
                match_type = "exact" if ref_norm in haystack else "mention"

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

    Ejemplos:
      'SL1430-2022' → ['SL1430-2022', 'SL-1430-2022', 'SL 1430', 'SL-1430']
      'SC11593/2018' → ['SC11593-2018', 'SC-11593-2018', 'SC 11593']
    """
    ref = citation_ref.strip().upper()
    variants = [ref]

    # Detectar pattern SL/SC/SP/STC/STL/STP + numero + año
    m = re.match(r"(SL|SC|SP|STC|STL|STP)[\s\-]*(\d{1,5})[\s/\-]+(\d{2,4})", ref)
    if m:
        prefix, num, yr = m.group(1), m.group(2), m.group(3)
        variants.extend([
            f"{prefix}{num}-{yr}",
            f"{prefix}-{num}-{yr}",
            f"{prefix} {num}",
            f"{prefix}{num}",
            f"sentencia {prefix}{num}",
        ])

    # Dedup
    seen = set()
    out = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out
