"""MCP Server · Función Pública (gestor normativo)."""
from __future__ import annotations

from .base import BaseMCPClient


class FuncpubClient(BaseMCPClient):
    server_name = "funcpub"
    max_per_minute = 10

    def _register_methods(self):
        return {
            "fetch_concepto": self.fetch_concepto,
            "search_decreto": self.search_decreto,
        }

    async def fetch_concepto(self, *, numero: int, año: int) -> dict:
        """Fetch concepto jurídico de Función Pública."""
        from urllib.parse import quote
        url = f"https://www.funcionpublica.gov.co/eva/gestornormativo/buscar.php?q={quote(f'concepto {numero} {año}')}"
        try:
            html = await self._http_get(url)
            return {
                "numero": numero,
                "año": año,
                "citation_ref": f"Concepto {numero} de {año} (FuncPub)",
                "fuente_url": url,
                "html_length": len(html),
            }
        except Exception as e:
            return {
                "numero": numero, "año": año,
                "fuente_url": url, "error": str(e)[:200],
            }

    async def search_decreto(self, *, numero: int, año: int) -> dict:
        from urllib.parse import quote
        url = f"https://www.funcionpublica.gov.co/eva/gestornormativo/buscar.php?q={quote(f'decreto {numero} de {año}')}"
        try:
            html = await self._http_get(url)
            return {
                "numero": numero,
                "año": año,
                "citation_ref": f"Decreto {numero} de {año}",
                "fuente_url": url,
                "html_length": len(html),
            }
        except Exception as e:
            return {
                "numero": numero, "año": año,
                "fuente_url": url, "error": str(e)[:200],
            }
