"""
Cliente para la API de datos.gov.co (Socrata SODA API).
Accede a:
  - SUIN-Juriscol: 87,000+ normas (dataset fiev-nid6)
  - Corte Constitucional: sentencias (dataset v2k4-2t8s)

API pública, sin autenticación requerida.
"""
import logging
from typing import Optional
import httpx

from .base_source import BaseLegalSource
from derogation.models import LiveSourceResult

logger = logging.getLogger(__name__)

# Dataset IDs en datos.gov.co
SUIN_JURISCOL_DATASET = "fiev-nid6"
CORTE_CC_DATASET = "v2k4-2t8s"
BASE_API_URL = "https://www.datos.gov.co/resource"

# Timeout para requests
TIMEOUT = httpx.Timeout(15.0, connect=10.0)


class DatosGovCoSource(BaseLegalSource):
    """
    Fuente legal: datos.gov.co (Socrata SODA API).
    Acceso a SUIN-Juriscol (normas) y Corte Constitucional (sentencias).
    """

    name = "datos_gov"
    description = "Datos Abiertos Colombia - SUIN-Juriscol + Corte Constitucional"
    base_url = "https://www.datos.gov.co"
    is_api = True

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=TIMEOUT)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # --- SUIN-Juriscol: Normas ---

    async def search(self, query: str, limit: int = 10, **kwargs) -> list[LiveSourceResult]:
        """Busca normas en SUIN-Juriscol via Socrata API.

        The dataset fiev-nid6 is a structured metadata catalog, NOT a full-text
        corpus: its only topic field (`materia`) contains a closed taxonomy of
        ~50 categories (e.g. "Laboral individual", "Tributario nacional"), not
        free keywords. Free-text search against it produced massive false
        positives — queries containing "ley" matched every "Leyes de homenaje..."
        entry, returning decretos about monuments for a labor-law question.

        Policy: only run SUIN's search when the caller supplies a structured
        filter (tipo + numero + anio, or at minimum tipo + anio). For bare
        natural-language queries, return empty and let the agent fall back to
        LLM-extracted norm references via `search_norma()`/`fetch_norm()`.

        Dataset fields: n_mero (not numero), a_o (not ano), subtipo (may be
        the literal string "NULL"), materia is pipe-delimited taxonomy.
        """
        tipo_filter = kwargs.get("tipo")
        numero_filter = kwargs.get("numero")
        anio_filter = kwargs.get("anio")

        if not tipo_filter:
            logger.debug(
                "SUIN search skipped (no tipo filter): dataset is metadata-only, "
                "free-text queries return garbage. Query was: %r", query[:80]
            )
            return []

        where_parts = [f"tipo='{tipo_filter.upper()}'"]
        if numero_filter:
            where_parts.append(f"n_mero='{numero_filter}'")
        if anio_filter:
            where_parts.append(f"a_o='{anio_filter}'")

        params = {"$limit": str(limit), "$where": " AND ".join(where_parts)}

        try:
            client = await self._get_client()
            url = f"{BASE_API_URL}/{SUIN_JURISCOL_DATASET}.json"
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            results = [self._row_to_result(item) for item in data]
            logger.info(
                "datos.gov.co SUIN: %d normas (tipo=%s, n=%s, a=%s)",
                len(results), tipo_filter, numero_filter, anio_filter,
            )
            return results

        except httpx.HTTPError as e:
            logger.error(f"Error consultando datos.gov.co SUIN: {e}")
            return []

    @staticmethod
    def _row_to_result(item: dict) -> LiveSourceResult:
        """Convert a raw SUIN row into a LiveSourceResult, handling quirks:
        - `subtipo` is sometimes the literal string "NULL" (not Python None).
        - `titulo` falls back to "<TIPO> <N> de <A>" when subtipo is unusable.
        """
        numero_str = item.get("n_mero", "") or item.get("numero", "") or ""
        anio_str = item.get("a_o", "") or item.get("ano", "") or ""
        tipo = item.get("tipo", "") or ""

        subtipo = item.get("subtipo", "") or ""
        if subtipo.strip().upper() in ("", "NULL", "NONE"):
            subtipo = ""

        titulo = subtipo or f"{tipo} {numero_str} de {anio_str}".strip()
        preview = titulo if subtipo else f"{tipo} {numero_str} de {anio_str}"

        return LiveSourceResult(
            source="datos_gov_suin",
            tipo=tipo,
            numero=int(numero_str) if numero_str.isdigit() else None,
            anio=int(anio_str) if anio_str.isdigit() else None,
            titulo=titulo,
            estado=item.get("vigencia", ""),
            url=None,
            preview=preview,
            metadata={
                "sector": item.get("sector", ""),
                "entidad": item.get("entidad", ""),
                "materia": item.get("materia", ""),
                "vigencia": item.get("vigencia", ""),
                "subtipo": subtipo,
            },
        )

    async def search_norma(self, tipo: str, numero: int, anio: int) -> list[dict]:
        """Busca una norma específica por tipo, número y año."""
        params = {
            "$where": f"tipo='{tipo.upper()}' AND n_mero='{numero}' AND a_o='{anio}'",
            "$limit": "5",
        }

        try:
            client = await self._get_client()
            url = f"{BASE_API_URL}/{SUIN_JURISCOL_DATASET}.json"
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Error buscando norma en datos.gov.co: {e}")
            return []

    async def fetch_norm(self, tipo: str, numero: int, anio: int) -> Optional[dict]:
        """Obtiene metadatos de una norma desde SUIN-Juriscol."""
        results = await self.search_norma(tipo, numero, anio)
        if not results:
            return None

        item = results[0]
        subtipo = (item.get("subtipo") or "").strip()
        if subtipo.upper() in ("NULL", "NONE"):
            subtipo = ""
        titulo = subtipo or f"{item.get('tipo', tipo)} {numero} de {anio}"

        return {
            "tipo": item.get("tipo", tipo),
            "numero": numero,
            "anio": anio,
            "titulo": titulo,
            "estado": item.get("vigencia", ""),
            "sector": item.get("sector", ""),
            "entidad": item.get("entidad", ""),
            "fuente_url": None,
            "texto_completo": None,  # SUIN solo tiene metadatos, no texto completo
            "metadata": item,
        }

    # --- Corte Constitucional: Sentencias ---

    async def search_sentencias_cc(self, query: str, limit: int = 10,
                                    tipo_sentencia: Optional[str] = None,
                                    desde: Optional[str] = None,
                                    sentencia_id: Optional[str] = None) -> list[LiveSourceResult]:
        """
        Busca sentencias de la Corte Constitucional.

        Dataset v2k4-2t8s is metadata-only: sentencia ID, type, magistrado,
        proceso, fecha, sala. No topic/theme/text field. Free-text `$q`
        queries are nearly useless for thematic searches (e.g. `$q=despido`
        returns 0 while `$q=tutela` returns matches only because the
        `proceso` field literally contains "Tutela").

        Policy: require a structured filter — explicit sentencia_id, a
        tipo_sentencia, or a date range. Topical searches should go through
        the Corte Constitucional relatoría scraper instead.

        Args:
            query: Texto de búsqueda (only used if structured filter present).
            limit: Máximo de resultados
            tipo_sentencia: T (tutela), C (constitucionalidad), SU (unificación)
            desde: Fecha desde (YYYY-MM-DD)
            sentencia_id: ID exacto (e.g. "SU-449/20", "T-001/92")
        """
        if not (sentencia_id or tipo_sentencia or desde):
            logger.debug(
                "Corte CC search skipped (no structured filter): dataset is "
                "metadata-only, free-text queries return ~0 results. "
                "Query was: %r", query[:80]
            )
            return []

        params = {"$limit": str(limit)}

        where_parts = []
        if sentencia_id:
            where_parts.append(f"sentencia='{sentencia_id}'")
        if tipo_sentencia:
            where_parts.append(f"sentencia_tipo='{tipo_sentencia.upper()}'")
        if desde:
            where_parts.append(f"fecha_sentencia>='{desde}'")
        if where_parts:
            params["$where"] = " AND ".join(where_parts)

        try:
            client = await self._get_client()
            url = f"{BASE_API_URL}/{CORTE_CC_DATASET}.json"
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            results = []
            for item in data:
                numero = item.get("sentencia", "")
                results.append(LiveSourceResult(
                    source="datos_gov_cc",
                    tipo=item.get("sentencia_tipo", ""),
                    numero=None,
                    anio=None,
                    titulo=f"Sentencia {numero}",
                    estado="PRECEDENTE" if item.get("sentencia_tipo") == "SU" else None,
                    url=None,
                    preview=f"Corte Constitucional - {numero} - Mag. {item.get('magistrado_a', '')}",
                    metadata={
                        "proceso": item.get("proceso", ""),
                        "expediente_tipo": item.get("expediente_tipo", ""),
                        "expediente_numero": item.get("expediente_numero", ""),
                        "magistrado": item.get("magistrado_a", ""),
                        "sala": item.get("sala", ""),
                        "fecha": item.get("fecha_sentencia", ""),
                    }
                ))

            logger.info(f"datos.gov.co CC: {len(results)} sentencias encontradas para '{query}'")
            return results

        except httpx.HTTPError as e:
            logger.error(f"Error consultando datos.gov.co CC: {e}")
            return []

    async def is_available(self) -> bool:
        try:
            client = await self._get_client()
            response = await client.get(
                f"{BASE_API_URL}/{SUIN_JURISCOL_DATASET}.json",
                params={"$limit": "1"}
            )
            return response.status_code == 200
        except Exception:
            return False
