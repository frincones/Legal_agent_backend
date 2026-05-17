"""Rama Judicial formatos scraper.

Source: https://www.ramajudicial.gov.co/ · cada despacho publica formatos
de demandas, recursos, solicitudes. La estructura es heterogénea (cada
juzgado expone su propia página estática).

Strategy: scrape a curated SEED LIST of known formato pages. Add more as
the curator (lawyer) identifies useful sources. This is intentionally
narrow at MVP — quality > quantity. The user can paste additional URLs
via the curator admin UI in Sprint 4.

Legal: público por mandato constitucional (art. 228 CPC). Sin restricciones
en robots.txt al momento de redactar este código (2026-05-17). Verificar
periódicamente.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Optional

from .base import TemplateCandidate, TemplateSourceBase

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 30.0

# Seed list of (URL, suggested_doc_type, suggested_materia, label).
# Expand via curator UI in Sprint 4. These are KNOWN public formatos.
SEED_URLS: list[tuple[str, str, str, str]] = [
    # Defensoría del Pueblo · tutela estándar
    (
        "https://www.defensoria.gov.co/web/guest/tutela",
        "tutela",
        "constitucional",
        "Acción de tutela · Defensoría del Pueblo",
    ),
    # Rama Judicial · derecho de petición (varios despachos publican formatos)
    (
        "https://www.ramajudicial.gov.co/web/derecho-de-peticion",
        "derecho_peticion",
        "administrativo",
        "Derecho de petición · Rama Judicial",
    ),
    # Demanda ejecutiva (formato estándar Juzgado Civil del Circuito)
    (
        "https://www.ramajudicial.gov.co/web/juzgados-civiles/formatos",
        "demanda_civil",
        "civil",
        "Demanda ejecutiva singular · formato estándar",
    ),
    # Recurso de reposición y apelación (administrativo)
    (
        "https://www.ramajudicial.gov.co/web/juzgados-administrativos/formatos",
        "recurso_reposicion",
        "administrativo",
        "Recurso de reposición · jurisdicción contencioso administrativa",
    ),
]


class RamaJudicialFormatosScraper(TemplateSourceBase):
    name = "rama_judicial"
    description = "Formatos oficiales publicados por la Rama Judicial CO"
    base_url = "https://www.ramajudicial.gov.co"
    request_delay_seconds = 2.0    # conservative · static gov pages

    def __init__(self, *, seed_urls: Optional[list[tuple[str, str, str, str]]] = None):
        self.seed_urls = seed_urls or SEED_URLS

    async def fetch(self, *, limit: int = 100) -> AsyncIterator[TemplateCandidate]:
        try:
            import httpx
        except ImportError:
            logger.error("httpx not installed · skipping Rama Judicial scraper")
            return

        emitted = 0
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
        ) as client:
            for url, doc_type, materia, label in self.seed_urls:
                if emitted >= limit:
                    break
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                except Exception as e:
                    logger.warning("rama_judicial · %s failed: %s", url, e)
                    continue

                content_type = resp.headers.get("content-type", "")
                if "text/html" in content_type:
                    body_text = self.html_to_markdown(resp.text)
                else:
                    # Skip non-HTML for now · PDF parsing comes later via Docling.
                    logger.info("rama_judicial · %s is %s · skipping", url, content_type)
                    continue

                if len(body_text) < 200:
                    logger.info("rama_judicial · %s too short · skipping", url)
                    continue

                cand = TemplateCandidate(
                    source=self.name,
                    source_ref=url,
                    source_url=url,
                    raw_text=resp.text[:50000],
                    normalized_md=f"# {label}\n\nFuente: {url}\n\n{body_text}",
                    suggested_materia=materia,
                    suggested_doc_type=doc_type,
                    metadata={"label": label},
                )
                yield cand
                emitted += 1
                await self.polite_sleep()
