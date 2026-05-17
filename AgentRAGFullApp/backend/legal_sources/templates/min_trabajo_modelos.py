"""MinTrabajo modelos scraper · PDFs y formatos publicados.

Source: https://www.mintrabajo.gov.co/normatividad/leyes/modelos-de-contratos
y secciones relacionadas (carta de despido, liquidación, etc.).

Estrategia: seed list de PDFs/HTMLs conocidos. Para PDFs usamos el reader
de docling existente (ya está en el pipeline de ingestion). En esta primera
iteración solo emitimos los HTMLs · los PDFs los procesa el batch
enrichment con docling_reader cuando lleguen a template_candidates.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Optional

from .base import TemplateCandidate, TemplateSourceBase

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 30.0

# (url, doc_type, subtype, label)
SEED_URLS: list[tuple[str, str, str, str]] = [
    (
        "https://www.mintrabajo.gov.co/normatividad/leyes/contrato-de-trabajo",
        "contrato",
        "contrato_trabajo_termino_indefinido",
        "Modelo de contrato de trabajo a término indefinido · MinTrabajo",
    ),
    (
        "https://www.mintrabajo.gov.co/normatividad/leyes/contrato-de-trabajo-termino-fijo",
        "contrato",
        "contrato_trabajo_termino_fijo",
        "Modelo de contrato de trabajo a término fijo · MinTrabajo",
    ),
    (
        "https://www.mintrabajo.gov.co/preguntas-frecuentes/carta-de-terminacion",
        "memorial",
        "carta_terminacion_laboral",
        "Carta de terminación de contrato laboral · MinTrabajo guía",
    ),
    (
        "https://www.mintrabajo.gov.co/preguntas-frecuentes/liquidacion-prestaciones-sociales",
        "memorial",
        "liquidacion_prestaciones_sociales",
        "Liquidación de prestaciones sociales · MinTrabajo guía",
    ),
]


class MinTrabajoScraper(TemplateSourceBase):
    name = "min_trabajo"
    description = "Modelos de documentos laborales · Ministerio del Trabajo CO"
    base_url = "https://www.mintrabajo.gov.co"
    request_delay_seconds = 2.0

    def __init__(self, *, seed_urls: Optional[list[tuple[str, str, str, str]]] = None):
        self.seed_urls = seed_urls or SEED_URLS

    async def fetch(self, *, limit: int = 100) -> AsyncIterator[TemplateCandidate]:
        try:
            import httpx
        except ImportError:
            logger.error("httpx not installed · skipping MinTrabajo scraper")
            return

        emitted = 0
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
        ) as client:
            for url, doc_type, subtype, label in self.seed_urls:
                if emitted >= limit:
                    break
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                except Exception as e:
                    logger.warning("min_trabajo · %s failed: %s", url, e)
                    continue

                content_type = resp.headers.get("content-type", "")
                if "text/html" not in content_type:
                    # PDFs land in raw bucket · enrichment pipeline parses later.
                    logger.info(
                        "min_trabajo · %s is %s · staging-only (no parse here)",
                        url, content_type,
                    )
                    continue

                body_text = self.html_to_markdown(resp.text)
                if len(body_text) < 200:
                    continue

                norms = self._guess_norms(body_text)

                cand = TemplateCandidate(
                    source=self.name,
                    source_ref=url,
                    source_url=url,
                    raw_text=resp.text[:50000],
                    normalized_md=f"# {label}\n\nFuente: {url}\n\n{body_text}",
                    suggested_materia="laboral",
                    suggested_doc_type=doc_type,
                    suggested_subtype=subtype,
                    suggested_norms=norms,
                    metadata={"label": label},
                )
                yield cand
                emitted += 1
                await self.polite_sleep()

    @staticmethod
    def _guess_norms(text: str) -> list[str]:
        """Cheap regex pass · catches the most common CST + Ley refs."""
        import re
        patterns = [
            r"(?:Código Sustantivo del Trabajo|CST)[^.]{0,80}art(?:ículo|\.)\s*(\d+)",
            r"Ley\s+(\d+)\s*(?:de\s*)?(\d{4})",
            r"Decreto\s+(\d+)\s*(?:de\s*)?(\d{4})",
        ]
        found: set[str] = set()
        for pat in patterns:
            for m in re.finditer(pat, text, flags=re.IGNORECASE):
                groups = [g for g in m.groups() if g]
                if "CST" in pat or "Código Sustantivo" in pat:
                    found.add(f"CST art. {groups[0]}")
                elif "Ley" in pat:
                    found.add(f"Ley {groups[0]}/{groups[1]}" if len(groups) >= 2 else f"Ley {groups[0]}")
                elif "Decreto" in pat:
                    found.add(f"Decreto {groups[0]}/{groups[1]}" if len(groups) >= 2 else f"Decreto {groups[0]}")
        return sorted(found)[:20]
