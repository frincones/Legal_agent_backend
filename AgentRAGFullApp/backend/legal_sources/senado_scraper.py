"""
Scraper para la Secretaría del Senado (basedoc).
URLs predecibles, HTML estático, sin API.
Fuente: http://www.secretariasenado.gov.co/senado/basedoc/

Patrones de URL:
  - Leyes: ley_{numero}_{año}.html
  - Decretos: decreto_{numero}_{año}.html
  - Códigos: codigo_sustantivo_trabajo.html
"""
import logging
import re
from typing import Optional
import httpx

from .base_source import BaseLegalSource
from .html_parser import parse_norm_html
from derogation.models import LiveSourceResult

logger = logging.getLogger(__name__)

BASE_URL = "http://www.secretariasenado.gov.co/senado/basedoc"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# Mapa de tipos de norma a prefijo de URL
TIPO_TO_PREFIX = {
    "LEY": "ley",
    "DECRETO": "decreto",
    "RESOLUCION": "resolucion",
    "ACUERDO": "acuerdo",
}

# Códigos conocidos con sus slugs
CODIGOS = {
    "CODIGO_SUSTANTIVO_TRABAJO": "codigo_sustantivo_trabajo",
    "CODIGO_CIVIL": "codigo_civil",
    "CODIGO_PENAL": "codigo_penal",
    "CODIGO_COMERCIO": "codigo_de_comercio",
    "CODIGO_PROCEDIMIENTO_CIVIL": "codigo_procedimiento_civil",
    "CONSTITUCION": "constitucion_politica_1991",
}


class SenadoSource(BaseLegalSource):
    """
    Fuente legal: Secretaría del Senado - Base Documental.
    HTML estático con URLs predecibles.
    """

    name = "senado"
    description = "Secretaría del Senado - Leyes, Decretos y Códigos de Colombia"
    base_url = BASE_URL
    is_api = False

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": "LegalAgentBot/1.0 (investigacion juridica)"}
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _build_urls(self, tipo: str, numero: int, anio: int) -> list[str]:
        """Build candidate URLs for a norm.

        Senado's basedoc zero-pads `numero` to 4 digits (ley_0789_2002.html,
        ley_0100_1993.html). Numbers ≥ 1000 already fit without padding
        (ley_1010_2006.html works as-is). The previous implementation built
        ley_789_2002.html (no padding), which returned 404 for every number
        < 1000 and wasted ~20s on the slow server.

        Returns a list ordered by likelihood: padded first, then unpadded
        (as fallback). Codes >= 1000 yield one URL since both forms collapse.
        """
        prefix = TIPO_TO_PREFIX.get(tipo.upper())
        if not prefix:
            return []
        padded = f"{BASE_URL}/{prefix}_{numero:04d}_{anio}.html"
        if numero >= 1000:
            return [padded]
        unpadded = f"{BASE_URL}/{prefix}_{numero}_{anio}.html"
        return [padded, unpadded]

    def _build_url(self, tipo: str, numero: int, anio: int) -> Optional[str]:
        """Primary (most likely) URL. Kept for backward compat callers."""
        urls = self._build_urls(tipo, numero, anio)
        return urls[0] if urls else None

    async def search(self, query: str, limit: int = 10, **kwargs) -> list[LiveSourceResult]:
        """
        Búsqueda en el Senado.
        Como no hay API de búsqueda, intenta extraer tipo/numero/año del query
        y construir la URL directa.
        """
        results = []

        # Intentar extraer referencia a norma del query
        patterns = [
            r"(?:ley|Ley|LEY)\s+(\d+)\s+(?:de\s+)?(\d{4})",
            r"(?:decreto|Decreto|DECRETO)\s+(\d+)\s+(?:de\s+)?(\d{4})",
            r"(?:resoluci[oó]n|Resoluci[oó]n|RESOLUCION)\s+(\d+)\s+(?:de\s+)?(\d{4})",
        ]

        tipo_map = {0: "LEY", 1: "DECRETO", 2: "RESOLUCION"}

        for i, pattern in enumerate(patterns):
            for match in re.finditer(pattern, query):
                numero = int(match.group(1))
                anio = int(match.group(2))
                tipo = tipo_map[i]
                for url in self._build_urls(tipo, numero, anio):
                    if await self._check_url(url):
                        results.append(LiveSourceResult(
                            source="senado",
                            tipo=tipo,
                            numero=numero,
                            anio=anio,
                            titulo=f"{tipo.title()} {numero} de {anio}",
                            url=url,
                            preview=f"Texto completo disponible en Secretaría del Senado",
                            metadata={"url_verified": True}
                        ))
                        break  # first working URL wins

                if len(results) >= limit:
                    break

        return results

    async def _check_url(self, url: str) -> bool:
        """Verifica si una URL existe (HEAD request)."""
        try:
            client = await self._get_client()
            response = await client.head(url)
            return response.status_code == 200
        except Exception:
            return False

    async def fetch_norm(self, tipo: str, numero: int, anio: int) -> Optional[dict]:
        """Obtiene el texto completo de una norma del Senado.

        Tries padded and unpadded URL variants; first 200 wins.
        """
        urls = self._build_urls(tipo, numero, anio)
        if not urls:
            return None

        client = await self._get_client()
        last_status = None

        for url in urls:
            try:
                response = await client.get(url)
                last_status = response.status_code
                logger.info(
                    "Senado fetch %s status=%d bytes=%d content_type=%s",
                    url, response.status_code, len(response.content),
                    response.headers.get("content-type", "?"),
                )
                if response.status_code != 200:
                    continue

                try:
                    parsed = parse_norm_html(response.text, source="senado")
                    logger.info("Senado %s parsed titulo='%s' chars=%d arts=%d",
                                url,
                                (parsed.get("titulo") or "")[:80],
                                len(parsed.get("texto_completo", "")),
                                len(parsed.get("articulos", [])))
                except Exception as parse_err:
                    logger.error("Senado parse_norm_html failed for %s: %s", url, parse_err)
                    parsed = {"titulo": "", "texto_completo": response.text[:500], "articulos": []}

                return {
                    "tipo": tipo.upper(),
                    "numero": numero,
                    "anio": anio,
                    "titulo": parsed.get("titulo", f"{tipo} {numero} de {anio}"),
                    "texto_completo": parsed.get("texto_completo", ""),
                    "articulos": parsed.get("articulos", []),
                    "fuente_url": url,
                    "fuente": "senado",
                    "metadata": {
                        "articulos_count": len(parsed.get("articulos", [])),
                        "char_count": len(parsed.get("texto_completo", "")),
                    }
                }
            except httpx.HTTPError as e:
                logger.warning(f"Senado fetch failed for {url} (HTTPError): {e}")
            except Exception as e:
                logger.error(f"Senado fetch unexpected error for {url}: {type(e).__name__}: {e}")

        logger.info(f"Senado: {tipo} {numero}/{anio} not available (last status={last_status})")
        return None

    async def fetch_codigo(self, codigo_key: str) -> Optional[dict]:
        """
        Obtiene un código completo (puede tener múltiples partes).

        Args:
            codigo_key: Key del diccionario CODIGOS (ej: "CODIGO_SUSTANTIVO_TRABAJO")
        """
        slug = CODIGOS.get(codigo_key.upper())
        if not slug:
            return None

        all_text = []
        all_articles = []

        # Los códigos pueden tener partes: _pr001.html, _pr002.html, etc.
        # Primero intentar el index
        index_url = f"{BASE_URL}/{slug}.html"

        try:
            client = await self._get_client()

            # Intentar partes numeradas
            for part in range(1, 10):
                part_url = f"{BASE_URL}/{slug}_pr{part:03d}.html"
                response = await client.get(part_url)
                if response.status_code != 200:
                    break

                parsed = parse_norm_html(response.text, source="senado")
                all_text.append(parsed.get("texto_completo", ""))
                all_articles.extend(parsed.get("articulos", []))

            # Si no encontró partes, intentar URL directa
            if not all_text:
                response = await client.get(index_url)
                if response.status_code == 200:
                    parsed = parse_norm_html(response.text, source="senado")
                    all_text.append(parsed.get("texto_completo", ""))
                    all_articles.extend(parsed.get("articulos", []))

            if not all_text:
                return None

            return {
                "tipo": "CODIGO",
                "titulo": codigo_key.replace("_", " ").title(),
                "texto_completo": "\n\n".join(all_text),
                "articulos": all_articles,
                "fuente_url": index_url,
                "fuente": "senado",
                "metadata": {
                    "partes": len(all_text),
                    "articulos_count": len(all_articles),
                }
            }

        except httpx.HTTPError as e:
            logger.error(f"Error obteniendo código del Senado: {e}")
            return None

    async def is_available(self) -> bool:
        try:
            client = await self._get_client()
            response = await client.head(f"{BASE_URL}/arbol/1000.html")
            return response.status_code == 200
        except Exception:
            return False
