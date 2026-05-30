"""MCP Server · Secretaría del Senado de la República (basedoc oficial)."""
from __future__ import annotations

from .base import BaseMCPClient


class SenadoClient(BaseMCPClient):
    server_name = "senado"
    max_per_minute = 10

    def _register_methods(self):
        return {
            "fetch_ley": self.fetch_ley,
            "search_proyecto": self.search_proyecto,
            "latest_publicadas": self.latest_publicadas,
        }

    async def fetch_ley(self, *, numero: int, año: int) -> dict:
        """Fetch Ley por número y año desde basedoc del Senado.

        URL canónica: https://www.secretariasenado.gov.co/senado/basedoc/ley_{numero:04d}_{año}.html
        """
        url = f"https://www.secretariasenado.gov.co/senado/basedoc/ley_{numero:04d}_{año}.html"
        try:
            html = await self._http_get(url)
            return {
                "numero": numero,
                "año": año,
                "citation_ref": f"Ley {numero} de {año}",
                "fuente_url": url,
                "exists": True,
                "html_length": len(html),
                "html_excerpt": html[:1500],
            }
        except Exception as e:
            return {
                "numero": numero, "año": año,
                "fuente_url": url, "exists": False, "error": str(e)[:200],
            }

    async def search_proyecto(self, *, numero: int) -> dict:
        from urllib.parse import quote
        url = f"https://www.secretariasenado.gov.co/index.php/proyectos-de-ley?q={quote(str(numero))}"
        try:
            html = await self._http_get(url)
            return {
                "numero": numero,
                "fuente_url": url,
                "html_length": len(html),
            }
        except Exception as e:
            return {"numero": numero, "fuente_url": url, "error": str(e)[:200]}

    async def latest_publicadas(self, *, tipo: str = "ley", limit: int = 10) -> dict:
        url = "https://www.secretariasenado.gov.co/index.php/leyes-recientes"
        try:
            html = await self._http_get(url)
            return {"tipo": tipo, "fuente_url": url, "html_length": len(html)}
        except Exception as e:
            return {"tipo": tipo, "fuente_url": url, "error": str(e)[:200]}
