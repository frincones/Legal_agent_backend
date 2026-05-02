"""
Scraper para el Gestor Normativo de Función Pública.
Fuente: https://www.funcionpublica.gov.co/eva/gestornormativo/

URLs:
  - Norma por ID: norma.php?i={ID}
  - PDF: norma_pdf.php?i={ID}
  - Búsqueda temática: consulta-tematica.php
"""
import logging
import re
from typing import Optional
import httpx

from .base_source import BaseLegalSource
from .html_parser import parse_norm_html
from derogation.models import LiveSourceResult

logger = logging.getLogger(__name__)

BASE_URL = "https://www.funcionpublica.gov.co/eva/gestornormativo"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# IDs conocidos de normas importantes (cache manual para acceso rápido)
KNOWN_NORM_IDS = {
    ("LEY", 1010, 2006): 18843,       # Acoso Laboral
    ("LEY", 2209, 2022): 186987,      # Modifica prescripción Ley 1010
    ("LEY", 2466, 2025): None,        # Reforma Laboral (buscar dinámicamente)
    ("RESOLUCION", 652, 2012): None,   # Comité Convivencia (derogada)
    ("RESOLUCION", 1356, 2012): None,  # Modifica Res 652 (derogada)
    ("RESOLUCION", 3461, 2025): 262916, # Nuevo Comité Convivencia
    ("LEY", 100, 1993): None,          # Seguridad Social
    ("LEY", 50, 1990): None,           # Reforma Laboral 1990
    ("LEY", 789, 2002): None,          # Reforma Laboral 2002
}


class FuncionPublicaSource(BaseLegalSource):
    """
    Fuente legal: Gestor Normativo de Función Pública.
    Acceso por ID numérico o scraping de búsqueda.
    """

    name = "funcion_publica"
    description = "Gestor Normativo - Función Pública de Colombia"
    base_url = BASE_URL
    is_api = False

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=TIMEOUT,
                follow_redirects=True,
                verify=False,  # SSL issues con funcionpublica.gov.co
                headers={"User-Agent": "LegalAgentBot/1.0 (investigacion juridica)"}
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def search(self, query: str, limit: int = 10, **kwargs) -> list[LiveSourceResult]:
        """
        Búsqueda en Función Pública.
        Intenta extraer normas del query y buscar por ID conocido.
        """
        results = []

        # Extraer referencias a normas del query
        patterns = [
            (r"(?:[Ll]ey)\s+(\d+)\s+(?:de\s+)?(\d{4})", "LEY"),
            (r"(?:[Dd]ecreto)\s+(\d+)\s+(?:de\s+)?(\d{4})", "DECRETO"),
            (r"(?:[Rr]esoluci[oó]n)\s+(\d+)\s+(?:de\s+)?(\d{4})", "RESOLUCION"),
        ]

        for pattern, tipo in patterns:
            for match in re.finditer(pattern, query):
                numero = int(match.group(1))
                anio = int(match.group(2))

                # Función Pública requires a numeric internal ID. We only
                # surface results for norms we've mapped manually — entries
                # without an ID have no URL to follow and just contaminate
                # the pipeline's result list.
                norm_id = KNOWN_NORM_IDS.get((tipo, numero, anio))
                if not norm_id:
                    continue

                results.append(LiveSourceResult(
                    source="funcion_publica",
                    tipo=tipo,
                    numero=numero,
                    anio=anio,
                    titulo=f"{tipo.title()} {numero} de {anio}",
                    url=f"{BASE_URL}/norma.php?i={norm_id}",
                    preview=f"Gestor Normativo - Función Pública (ID: {norm_id})",
                    metadata={"norm_id": norm_id, "has_full_text": True},
                ))

                if len(results) >= limit:
                    break

        return results

    async def fetch_norm(self, tipo: str, numero: int, anio: int) -> Optional[dict]:
        """Obtiene el texto completo de una norma de Función Pública."""
        # Buscar ID conocido
        norm_id = KNOWN_NORM_IDS.get((tipo.upper(), numero, anio))

        if not norm_id:
            logger.info(f"No hay ID conocido para {tipo} {numero} de {anio} en Función Pública")
            return None

        return await self.fetch_by_id(norm_id, tipo, numero, anio)

    async def fetch_by_id(self, norm_id: int, tipo: str = "", numero: int = 0, anio: int = 0) -> Optional[dict]:
        """Obtiene una norma por su ID interno de Función Pública."""
        url = f"{BASE_URL}/norma.php?i={norm_id}"

        try:
            client = await self._get_client()
            response = await client.get(url)

            if response.status_code != 200:
                logger.warning(f"Función Pública: {url} returned {response.status_code}")
                return None

            html = response.text
            parsed = parse_norm_html(html, source="funcion_publica")

            # Extraer notas de vigencia (Función Pública las incluye)
            notas_vigencia = self._extract_vigencia_notes(html)

            return {
                "tipo": tipo.upper() or parsed.get("tipo", ""),
                "numero": numero or parsed.get("numero"),
                "anio": anio or parsed.get("anio"),
                "titulo": parsed.get("titulo", f"{tipo} {numero} de {anio}"),
                "texto_completo": parsed.get("texto_completo", ""),
                "articulos": parsed.get("articulos", []),
                "notas_vigencia": notas_vigencia,
                "fuente_url": url,
                "fuente_id": f"i={norm_id}",
                "fuente": "funcion_publica",
                "metadata": {
                    "norm_id": norm_id,
                    "notas_vigencia_count": len(notas_vigencia),
                    "articulos_count": len(parsed.get("articulos", [])),
                }
            }

        except httpx.HTTPError as e:
            logger.error(f"Error obteniendo norma de Función Pública: {e}")
            return None

    def _extract_vigencia_notes(self, html: str) -> list[str]:
        """Extrae notas de vigencia del HTML de Función Pública."""
        notes = []
        # Función Pública marca notas de vigencia con clases CSS específicas
        patterns = [
            r'<[^>]*class="[^"]*vigencia[^"]*"[^>]*>(.*?)</[^>]+>',
            r'<[^>]*class="[^"]*nota[^"]*"[^>]*>(.*?)</[^>]+>',
            r'\[Nota de vigencia[^]]*\]',
            r'(?:Derogado|Modificado|Adicionado)\s+por\s+(?:el|la)\s+(?:art[ií]culo\s+\d+\s+(?:de\s+)?)?(?:la|el)\s+\w+\s+\d+\s+de\s+\d{4}',
        ]

        for pattern in patterns:
            for match in re.finditer(pattern, html, re.IGNORECASE | re.DOTALL):
                note = re.sub(r'<[^>]+>', '', match.group(0)).strip()
                if note and len(note) > 10:
                    notes.append(note[:500])

        return notes

    async def get_pdf_url(self, norm_id: int) -> str:
        """Retorna la URL del PDF de una norma."""
        return f"{BASE_URL}/norma_pdf.php?i={norm_id}"

    async def is_available(self) -> bool:
        try:
            client = await self._get_client()
            response = await client.head(f"{BASE_URL}/norma.php?i=18843")
            return response.status_code == 200
        except Exception:
            return False
