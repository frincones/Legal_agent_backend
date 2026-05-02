"""
Orquestador de búsqueda en múltiples fuentes legales.
Ejecuta búsquedas en paralelo y unifica resultados.
"""
import asyncio
import logging
import time
from typing import Optional

from derogation.models import LiveSourceResult
from .datos_gov_co import DatosGovCoSource
from .senado_scraper import SenadoSource
from .funcion_publica import FuncionPublicaSource
from .corte_constitucional import CorteConstitucionalSource

logger = logging.getLogger(__name__)


class LegalSourceRouter:
    """
    Orquesta búsquedas en múltiples fuentes legales colombianas.
    Ejecuta en paralelo con timeout y unifica resultados.
    """

    def __init__(self, config: Optional[dict] = None):
        config = config or {}
        self.timeout = config.get("search_timeout_seconds", 15)
        self.max_concurrent = config.get("max_concurrent_sources", 3)

        # Inicializar fuentes habilitadas
        self.sources = {}
        if config.get("datos_gov_co", True):
            self.sources["datos_gov"] = DatosGovCoSource()
        if config.get("senado", True):
            self.sources["senado"] = SenadoSource()
        if config.get("funcion_publica", True):
            self.sources["funcion_publica"] = FuncionPublicaSource()
        if config.get("corte_constitucional", True):
            self.sources["corte_cc"] = CorteConstitucionalSource()

    async def search(self, query: str, limit: int = 10,
                     sources: Optional[list[str]] = None,
                     **kwargs) -> dict:
        """
        Busca en múltiples fuentes en paralelo.

        Args:
            query: Texto de búsqueda
            limit: Máximo resultados por fuente
            sources: Lista de fuentes a consultar (None = todas)

        Returns:
            dict con keys: results, sources_consulted, duration_ms, errors
        """
        start = time.time()

        # Filtrar fuentes solicitadas
        active_sources = {}
        for name, source in self.sources.items():
            if sources is None or name in sources:
                active_sources[name] = source

        if not active_sources:
            return {"results": [], "sources_consulted": [], "duration_ms": 0, "errors": []}

        # Crear tareas de búsqueda
        semaphore = asyncio.Semaphore(self.max_concurrent)
        tasks = []

        for name, source in active_sources.items():
            tasks.append(self._search_with_timeout(name, source, query, limit, semaphore, **kwargs))

        # Ejecutar en paralelo
        task_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Unificar resultados
        all_results = []
        sources_consulted = []
        errors = []

        for name, result in zip(active_sources.keys(), task_results):
            if isinstance(result, Exception):
                errors.append({"source": name, "error": str(result)})
                logger.error(f"Error en fuente {name}: {result}")
            elif isinstance(result, list):
                all_results.extend(result)
                sources_consulted.append(name)
            else:
                sources_consulted.append(name)

        duration_ms = int((time.time() - start) * 1000)

        logger.info(
            f"Búsqueda legal: {len(all_results)} resultados de {len(sources_consulted)} fuentes en {duration_ms}ms"
        )

        return {
            "results": all_results,
            "sources_consulted": sources_consulted,
            "duration_ms": duration_ms,
            "errors": errors,
        }

    async def _search_with_timeout(self, name: str, source, query: str,
                                     limit: int, semaphore: asyncio.Semaphore,
                                     **kwargs) -> list[LiveSourceResult]:
        """Ejecuta búsqueda con semáforo y timeout."""
        async with semaphore:
            try:
                return await asyncio.wait_for(
                    source.search(query, limit=limit, **kwargs),
                    timeout=self.timeout
                )
            except asyncio.TimeoutError:
                logger.warning(f"Timeout en fuente {name} ({self.timeout}s)")
                return []
            except Exception as e:
                logger.error(f"Error en fuente {name}: {e}")
                return []

    async def fetch_norm(self, tipo: str, numero: int, anio: int,
                          preferred_source: Optional[str] = None) -> Optional[dict]:
        """
        Obtiene el texto completo de una norma.
        Intenta primero la fuente preferida, luego las demás.

        Args:
            tipo: LEY, DECRETO, RESOLUCION
            numero: Número de la norma
            anio: Año
            preferred_source: Fuente preferida ("senado", "funcion_publica")

        Returns:
            dict con texto completo o None
        """
        # Orden de intentos
        source_order = []
        if preferred_source and preferred_source in self.sources:
            source_order.append(preferred_source)
        # Agregar el resto
        for name in ["senado", "funcion_publica", "corte_cc", "datos_gov"]:
            if name in self.sources and name not in source_order:
                source_order.append(name)

        for name in source_order:
            source = self.sources[name]
            try:
                result = await asyncio.wait_for(
                    source.fetch_norm(tipo, numero, anio),
                    timeout=self.timeout
                )
                if result and result.get("texto_completo"):
                    logger.info(f"Norma {tipo} {numero}/{anio} obtenida de {name}")
                    return result
            except asyncio.TimeoutError:
                logger.warning(f"Timeout obteniendo norma de {name}")
            except Exception as e:
                logger.error(f"Error obteniendo norma de {name}: {e}")

        return None

    async def search_sentencias(self, query: str, limit: int = 10,
                                 tipo_sentencia: Optional[str] = None,
                                 desde: Optional[str] = None) -> list[LiveSourceResult]:
        """Busca sentencias en las fuentes de jurisprudencia."""
        results = []

        # datos.gov.co - Corte Constitucional
        if "datos_gov" in self.sources:
            try:
                cc_results = await self.sources["datos_gov"].search_sentencias_cc(
                    query, limit=limit, tipo_sentencia=tipo_sentencia, desde=desde
                )
                results.extend(cc_results)
            except Exception as e:
                logger.error(f"Error buscando sentencias en datos.gov.co: {e}")

        # Corte Constitucional directa
        if "corte_cc" in self.sources:
            try:
                cc_direct = await self.sources["corte_cc"].search(query, limit=limit)
                results.extend(cc_direct)
            except Exception as e:
                logger.error(f"Error buscando sentencias en Corte CC: {e}")

        return results

    async def check_sources_health(self) -> dict[str, bool]:
        """Verifica disponibilidad de todas las fuentes."""
        health = {}
        for name, source in self.sources.items():
            try:
                available = await asyncio.wait_for(source.is_available(), timeout=5)
                health[name] = available
            except Exception:
                health[name] = False
        return health

    async def close(self):
        """Cierra todas las conexiones HTTP."""
        for source in self.sources.values():
            if hasattr(source, "close"):
                try:
                    await source.close()
                except Exception:
                    pass
