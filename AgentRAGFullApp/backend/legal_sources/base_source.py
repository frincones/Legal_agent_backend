"""
Interfaz base para todas las fuentes legales.
"""
import logging
from abc import ABC, abstractmethod
from typing import Optional
from derogation.models import LiveSourceResult

logger = logging.getLogger(__name__)


class BaseLegalSource(ABC):
    """Interfaz base que todas las fuentes legales deben implementar."""

    name: str = "base"
    description: str = ""
    base_url: str = ""
    is_api: bool = False  # True = REST API, False = scraping HTML

    @abstractmethod
    async def search(self, query: str, limit: int = 10, **kwargs) -> list[LiveSourceResult]:
        """
        Busca en la fuente legal.

        Args:
            query: Texto de búsqueda
            limit: Máximo de resultados
            **kwargs: Filtros adicionales (tipo, año, etc.)

        Returns:
            Lista de resultados normalizados
        """
        ...

    @abstractmethod
    async def fetch_norm(self, tipo: str, numero: int, anio: int) -> Optional[dict]:
        """
        Obtiene el texto completo de una norma específica.

        Args:
            tipo: LEY, DECRETO, RESOLUCION, etc.
            numero: Número de la norma
            anio: Año de expedición

        Returns:
            Dict con texto_completo, titulo, fuente_url, metadata, o None si no encuentra
        """
        ...

    async def is_available(self) -> bool:
        """Verifica si la fuente está disponible (health check)."""
        return True

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name}>"
