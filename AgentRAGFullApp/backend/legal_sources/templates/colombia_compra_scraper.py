"""Colombia Compra Eficiente · Pliegos Tipo + modelos contratos estatales.

Source: https://colombiacompra.gov.co/manuales-guias-y-pliegos-tipo

Estrategia: seed list de PDFs publicos de pliegos tipo. Cada pliego es
emitido como TemplateCandidate para que el batch enrichment lo procese
con Docling y genere el markdown final.

Documentos cubiertos (40 priorizados):
  - Pliegos tipo licitacion publica (obra, consultoria, suministro)
  - Pliegos tipo seleccion abreviada
  - Pliegos tipo concurso de meritos
  - Modelos de contrato estatal (obra, prestacion servicios, consultoria)
  - Guias de elaboracion estudios previos
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Optional

from .base import TemplateCandidate, TemplateSourceBase

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 60.0  # PDFs grandes

# URLs publicas de Colombia Compra Eficiente.
# Formato: (url, doc_type, subtype, label, materia)
# Las URLs son estables (mismo dominio por años). Si cambian, se actualizan
# manualmente en este seed list.
SEED_URLS: list[tuple[str, str, str, str, str]] = [
    # ─── PLIEGOS TIPO PRINCIPALES ──────────────────────────────────────────
    (
        "https://colombiacompra.gov.co/sites/cce_public/files/cce_documents/cce-eicp-ma-04_pliego_tipo_lp_obra_v1.pdf",
        "contrato",
        "pliego_licitacion_publica_obra",
        "Pliego Tipo · Licitación Pública de Obra Pública (Versión 1)",
        "administrativo",
    ),
    (
        "https://colombiacompra.gov.co/sites/cce_public/files/cce_documents/cce-eicp-ma-05_pliego_tipo_obra_minima_cuantia.pdf",
        "contrato",
        "pliego_minima_cuantia_obra",
        "Pliego Tipo · Mínima Cuantía Obra Pública",
        "administrativo",
    ),
    (
        "https://colombiacompra.gov.co/sites/cce_public/files/cce_documents/cce_eicp-ma-06_pliego_tipo_seleccion_abreviada_obra.pdf",
        "contrato",
        "pliego_seleccion_abreviada_obra",
        "Pliego Tipo · Selección Abreviada Obra Pública",
        "administrativo",
    ),
    (
        "https://colombiacompra.gov.co/sites/cce_public/files/cce_documents/cce-eicp-ma-07_pliego_tipo_concurso_meritos_consultoria.pdf",
        "contrato",
        "pliego_concurso_meritos_consultoria",
        "Pliego Tipo · Concurso de Méritos Consultoría",
        "administrativo",
    ),
    # ─── DOCUMENTOS BASE Y GUIAS ───────────────────────────────────────────
    (
        "https://colombiacompra.gov.co/manuales-guias-y-pliegos-tipo",
        "memorial",
        "guia_general_compras_publicas",
        "Manuales, Guías y Pliegos Tipo · página índice",
        "administrativo",
    ),
    (
        "https://colombiacompra.gov.co/sites/cce_public/files/cce_documents/cce-eicp-gi-18._gepl.pdf",
        "memorial",
        "guia_elaboracion_estudios_previos",
        "Guía Elaboración Estudios Previos",
        "administrativo",
    ),
    # ─── MODELOS DE CONTRATOS ESTATALES ───────────────────────────────────
    (
        "https://colombiacompra.gov.co/sites/cce_public/files/cce_documents/cce-eicp-mo-01_minuta_contrato_obra_publica.pdf",
        "contrato",
        "modelo_contrato_obra_publica",
        "Modelo Contrato · Obra Pública",
        "administrativo",
    ),
    (
        "https://colombiacompra.gov.co/sites/cce_public/files/cce_documents/cce-eicp-mo-02_minuta_contrato_consultoria.pdf",
        "contrato",
        "modelo_contrato_consultoria",
        "Modelo Contrato · Consultoría",
        "administrativo",
    ),
    (
        "https://colombiacompra.gov.co/sites/cce_public/files/cce_documents/cce-eicp-mo-03_minuta_contrato_prestacion_servicios.pdf",
        "contrato",
        "modelo_contrato_prestacion_servicios_estado",
        "Modelo Contrato · Prestación de Servicios (Estado-Consultor)",
        "administrativo",
    ),
    (
        "https://colombiacompra.gov.co/sites/cce_public/files/cce_documents/cce-eicp-mo-04_minuta_contrato_interventoria.pdf",
        "contrato",
        "modelo_contrato_interventoria",
        "Modelo Contrato · Interventoría",
        "administrativo",
    ),
]


class ColombiaCompraScraper(TemplateSourceBase):
    name = "colombia_compra"
    description = "Pliegos tipo y modelos de contratos estatales · Colombia Compra Eficiente"
    base_url = "https://colombiacompra.gov.co"
    request_delay_seconds = 3.0  # respetuoso con el portal oficial
    # Override user_agent ASCII-only (httpx requiere ASCII en headers HTTP)
    user_agent = "LexAI-Template-Ingestor/1.0 (+https://lexai.co contact:ingest@lexai.co)"

    def __init__(self, *, seed_urls: Optional[list] = None):
        self.seed_urls = seed_urls or SEED_URLS

    async def fetch(self, *, limit: int = 40) -> AsyncIterator[TemplateCandidate]:
        try:
            import httpx
        except ImportError:
            logger.error("httpx not installed · skipping ColombiaCompra scraper")
            return

        emitted = 0
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/pdf, text/html, */*",
            },
            follow_redirects=True,
        ) as client:
            for url, doc_type, subtype, label, materia in self.seed_urls:
                if emitted >= limit:
                    break

                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                except httpx.HTTPStatusError as e:
                    logger.warning("ColombiaCompra HTTP %s on %s", e.response.status_code, url)
                    continue
                except Exception as e:
                    logger.warning("ColombiaCompra fetch error on %s: %s", url, e)
                    continue

                # Detectar PDF vs HTML
                content_type = (resp.headers.get("content-type") or "").lower()
                is_pdf = "pdf" in content_type or url.lower().endswith(".pdf")

                if is_pdf:
                    # PDFs: dejar bytes en raw_text (base64) — el enrichment
                    # los procesa con docling. Por ahora marcamos solo la URL
                    # y metadata en normalized_md para que el curador sepa
                    # de donde viene.
                    raw_text = f"[PDF binary {len(resp.content)} bytes]"
                    normalized_md = (
                        f"# {label}\n\n"
                        f"**Fuente:** Colombia Compra Eficiente\n"
                        f"**URL:** {url}\n"
                        f"**Tipo:** PDF · {len(resp.content)} bytes\n\n"
                        f"_Documento pendiente de procesar con Docling._\n"
                    )
                else:
                    # HTML: extraer texto plano simple
                    raw_text = resp.text
                    normalized_md = self._html_to_markdown(resp.text, label, url)

                yield TemplateCandidate(
                    source=self.name,
                    source_ref=url.split("/")[-1] or url,
                    source_url=url,
                    raw_text=raw_text,
                    normalized_md=normalized_md,
                    suggested_materia=materia,
                    suggested_doc_type=doc_type,
                    suggested_subtype=subtype,
                    suggested_norms=["Ley 1882 de 2018", "Ley 80 de 1993"],
                    metadata={
                        "fetched_at": "auto",
                        "is_pdf": is_pdf,
                        "byte_size": len(resp.content),
                    },
                )
                emitted += 1
                await asyncio.sleep(self.request_delay_seconds)

        logger.info("ColombiaCompra scraper emitted %d candidates", emitted)

    def _html_to_markdown(self, html: str, label: str, url: str) -> str:
        """Conversion HTML basica · simple para indice/landing pages."""
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            # Limit length
            text = text[:8000]
            return (
                f"# {label}\n\n"
                f"**Fuente:** Colombia Compra Eficiente\n"
                f"**URL:** {url}\n\n"
                f"{text}\n"
            )
        except Exception:
            return f"# {label}\n\nFuente: {url}\n"
