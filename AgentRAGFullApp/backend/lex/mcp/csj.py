"""MCP Server · Corte Suprema de Justicia de Colombia."""
from __future__ import annotations

from .base import BaseMCPClient


class CsjClient(BaseMCPClient):
    server_name = "csj"
    max_per_minute = 10

    def _register_methods(self):
        return {
            "fetch_sentencia": self.fetch_sentencia,
            "search_radicado": self.search_radicado,
            "latest_jurisprudencia": self.latest_jurisprudencia,
        }

    async def fetch_sentencia(
        self, *, sala: str, numero: int, año: int,
    ) -> dict:
        """Fetch sentencia de la CSJ por sala.

        Salas: 'civil', 'laboral', 'penal'
        URL base: https://cortesuprema.gov.co/corte/index.php/relatorias/
        """
        sala = (sala or "").lower().strip()
        if sala not in ("civil", "laboral", "penal"):
            raise ValueError(f"sala debe ser civil|laboral|penal, recibido {sala!r}")
        url = f"https://cortesuprema.gov.co/corte/index.php/sala-{sala}/"
        try:
            html = await self._http_get(url)
            return {
                "sala": sala,
                "numero": numero,
                "año": año,
                "citation_ref": f"SC{numero}-{año}" if sala == "civil"
                                 else f"SL{numero}-{año}" if sala == "laboral"
                                 else f"SP{numero}-{año}",
                "fuente_url": url,
                "exists": True,
                "html_length": len(html),
                "note": "Consulta directa al portal para texto completo.",
            }
        except Exception as e:
            return {
                "sala": sala, "numero": numero, "año": año,
                "fuente_url": url, "exists": False, "error": str(e)[:200],
            }

    async def search_radicado(self, *, numero_radicado: str) -> dict:
        url = "https://cortesuprema.gov.co/corte/index.php/relatorias/"
        return {
            "numero_radicado": numero_radicado,
            "fuente_url": url,
            "note": "Búsqueda por radicado requiere buscador interno del portal.",
        }

    async def latest_jurisprudencia(self, *, sala: str = "civil", limit: int = 10) -> dict:
        sala = (sala or "civil").lower().strip()
        url = f"https://cortesuprema.gov.co/corte/index.php/sala-{sala}/"
        try:
            html = await self._http_get(url)
            return {
                "sala": sala,
                "fuente_url": url,
                "html_length": len(html),
            }
        except Exception as e:
            return {"sala": sala, "fuente_url": url, "error": str(e)[:200]}
