"""
Scraper para la Relatoria de la Corte Constitucional.
Fuente: https://www.corteconstitucional.gov.co/relatoria/

URLs de sentencias:
  - https://www.corteconstitucional.gov.co/relatoria/{AÑO}/{tipo}-{numero}-{yy}.htm
  - Tipos: t (tutela), c (constitucionalidad), su (unificación)
"""
import logging
import re
from typing import Optional
import httpx

from .base_source import BaseLegalSource
from derogation.models import LiveSourceResult

logger = logging.getLogger(__name__)

BASE_URL = "https://www.corteconstitucional.gov.co/relatoria"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class CorteConstitucionalSource(BaseLegalSource):
    """
    Fuente legal: Relatoria de la Corte Constitucional.
    Sentencias con URLs predecibles.
    """

    name = "corte_cc"
    description = "Corte Constitucional de Colombia - Relatoria de Sentencias"
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

    # Empty/homepage responses share this size; used to reject soft-404s.
    _EMPTY_RESPONSE_MAX_BYTES = 15_000

    def _build_sentencia_urls(self, tipo: str, numero: int, anio: int) -> list[str]:
        """Candidate URLs for a sentencia.

        The relatoría uses different URL conventions per type:
          - T/C/A: `{t}-{NNN}-{yy}.htm` (hyphen-separated, e.g. t-067-25.htm)
          - SU:    `SU{NNN}-{yy}.htm`   (NO hyphen after SU, uppercase —
                                          `su-449-20.htm` returns the
                                          homepage, `su449-20.htm` or
                                          `SU449-20.htm` returns the real
                                          sentencia)

        The server returns HTTP 200 with a generic homepage (~8.6KB) for
        unknown paths, so URL validity must be confirmed by content size.
        Callers should try each candidate until one yields a response past
        the empty-page threshold.
        """
        tipo_upper = tipo.upper().strip()
        tipo_lower = tipo_upper.lower()
        yy = str(anio)[-2:]
        numero_str = f"{numero:03d}" if numero < 1000 else str(numero)
        prefix = f"{BASE_URL}/{anio}"

        if tipo_upper == "SU":
            return [
                f"{prefix}/SU{numero_str}-{yy}.htm",
                f"{prefix}/su{numero_str}-{yy}.htm",
            ]
        return [
            f"{prefix}/{tipo_upper}-{numero_str}-{yy}.htm",
            f"{prefix}/{tipo_lower}-{numero_str}-{yy}.htm",
        ]

    def _build_sentencia_url(self, tipo: str, numero: int, anio: int) -> str:
        """Backward-compat: primary (most likely) URL."""
        urls = self._build_sentencia_urls(tipo, numero, anio)
        return urls[0] if urls else ""

    def _is_real_sentencia(self, html: str) -> bool:
        """Reject soft-404 homepage responses (200 with ~8.6KB generic page)."""
        if len(html) <= self._EMPTY_RESPONSE_MAX_BYTES:
            return False
        # Real sentencia pages have "Sentencia" in <title>; homepage has just
        # "CORTE CONSTITUCIONAL DE COLOMBIA". We still size-gate first since
        # some mirrors omit <title>.
        title_match = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
        if title_match and "sentencia" not in title_match.group(1).lower():
            return False
        return True

    async def search(self, query: str, limit: int = 10, **kwargs) -> list[LiveSourceResult]:
        """
        Búsqueda de sentencias.
        Extrae referencias a sentencias del query (T-388-2019, C-200-19, SU-123-25).
        """
        results = []

        # Patrones de referencia a sentencias
        patterns = [
            r"(?:sentencia\s+)?([TCStcsu]{1,2})-(\d{1,4})[/-](?:de\s+)?(\d{2,4})",
            r"(?:sentencia\s+)?([TCStcsu]{1,2})\s+(\d{1,4})\s+(?:de|del)\s+(\d{4})",
        ]

        for pattern in patterns:
            for match in re.finditer(pattern, query, re.IGNORECASE):
                tipo = match.group(1).upper()
                numero = int(match.group(2))
                anio_str = match.group(3)
                anio = int(anio_str) if len(anio_str) == 4 else (2000 + int(anio_str) if int(anio_str) < 50 else 1900 + int(anio_str))

                url = self._build_sentencia_url(tipo, numero, anio)

                # Sentencia labeling: SU uses "SU-449/20" (slash + hyphen
                # in human form even though URL has no hyphen), T/C use
                # "T-388-2019" style with full year.
                label = f"{tipo}{'' if tipo == 'SU' else '-'}{numero}-{anio_str}"

                results.append(LiveSourceResult(
                    source="corte_cc",
                    tipo=tipo,
                    numero=numero,
                    anio=anio,
                    titulo=f"Sentencia {label}",
                    url=url,
                    preview=f"Corte Constitucional - Sentencia {label}",
                    metadata={"url": url}
                ))

                if len(results) >= limit:
                    break

        return results

    async def fetch_norm(self, tipo: str, numero: int, anio: int) -> Optional[dict]:
        """Obtiene el texto completo de una sentencia."""
        return await self.fetch_sentencia(tipo, numero, anio)

    async def fetch_sentencia(self, tipo: str, numero: int, anio: int) -> Optional[dict]:
        """Obtiene el texto completo de una sentencia de la Corte Constitucional.

        Tries each candidate URL and validates the response isn't the
        generic homepage soft-404.
        """
        urls = self._build_sentencia_urls(tipo, numero, anio)
        if not urls:
            return None

        client = await self._get_client()
        for url in urls:
            try:
                response = await client.get(url)
                # Diagnostic log · upgraded from debug to info so producción Railway logs lo guarden
                logger.info(
                    "Corte CC fetch %s status=%d bytes=%d content_type=%s",
                    url, response.status_code, len(response.content),
                    response.headers.get("content-type", "?"),
                )
                if response.status_code != 200:
                    continue

                html = response.text
                title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
                title_preview = (title_match.group(1)[:80] if title_match else "(no title)")
                logger.info("Corte CC %s html_len=%d title='%s'", url, len(html), title_preview.strip())
                if not self._is_real_sentencia(html):
                    logger.warning(
                        "Corte CC: %s rejected as soft-404 (size=%d title='%s')",
                        url, len(html), title_preview.strip(),
                    )
                    continue

                texto = self._clean_sentencia_html(html)
                titulo = self._extract_titulo(html, tipo, numero, anio)
                magistrado = self._extract_magistrado(html)

                return {
                    "corte": "CORTE_CONSTITUCIONAL",
                    "tipo_sentencia": tipo.upper(),
                    "numero": f"{tipo.upper()}-{numero}-{str(anio)[-2:]}",
                    "anio": anio,
                    "titulo": titulo,
                    "magistrado": magistrado,
                    "texto_completo": texto,
                    "fuente_url": url,
                    "fuente": "corte_cc",
                    "metadata": {
                        "char_count": len(texto),
                    }
                }

            except httpx.HTTPError as e:
                logger.warning(f"Corte CC: http error on {url}: {e}")

        logger.info(f"Corte CC: sentencia {tipo}-{numero}/{anio} not available at any candidate URL")
        return None

    def _clean_sentencia_html(self, html: str) -> str:
        """Extrae texto limpio de una sentencia HTML."""
        # Remover scripts y estilos
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        # Remover tags HTML
        text = re.sub(r'<[^>]+>', ' ', text)
        # Limpiar espacios
        text = re.sub(r'\s+', ' ', text).strip()
        # Remover caracteres especiales
        text = text.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
        return text

    def _extract_titulo(self, html: str, tipo: str, numero: int, anio: int) -> str:
        """Intenta extraer el título/tema de la sentencia."""
        # Buscar en tags de título
        match = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
        if match:
            titulo = re.sub(r'<[^>]+>', '', match.group(1)).strip()
            if titulo and len(titulo) > 5:
                return titulo
        return f"Sentencia {tipo.upper()}-{numero} de {anio}"

    def _extract_magistrado(self, html: str) -> Optional[str]:
        """Intenta extraer el magistrado ponente."""
        patterns = [
            r'[Mm]agistrado\s+[Pp]onente[:\s]+([A-ZÁÉÍÓÚÑ\s]+)',
            r'[Pp]onente[:\s]+([A-ZÁÉÍÓÚÑ\s]+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                return match.group(1).strip()[:100]
        return None

    async def is_available(self) -> bool:
        try:
            client = await self._get_client()
            response = await client.head(f"{BASE_URL}/")
            return response.status_code == 200
        except Exception:
            return False
