"""MCP Server · Corte Constitucional de Colombia.

Tools expuestas:
  - fetch_sentencia(tipo: "T"|"C"|"SU", numero: int, año: int)
  - search_tema(tema: str, limit: int = 10)
  - latest_sentencias(sala: str, limit: int = 10)
"""
from __future__ import annotations

from .base import BaseMCPClient


class CorteCcClient(BaseMCPClient):
    server_name = "corte_cc"
    max_per_minute = 10

    def _register_methods(self):
        return {
            "fetch_sentencia": self.fetch_sentencia,
            "search_tema": self.search_tema,
            "latest_sentencias": self.latest_sentencias,
        }

    async def fetch_sentencia(self, *, tipo: str, numero: int, año: int) -> dict:
        """Fetch sentencia T/C/SU por número y año.

        URL canónica: https://www.corteconstitucional.gov.co/relatoria/{año}/{tipo}-{num}-{año}.htm
        """
        tipo_u = (tipo or "").upper().strip()
        if tipo_u not in ("T", "C", "SU"):
            raise ValueError(f"tipo debe ser T|C|SU, recibido {tipo!r}")
        url = f"https://www.corteconstitucional.gov.co/relatoria/{año}/{tipo_u}-{numero:03d}-{año}.htm"
        try:
            html = await self._http_get(url)
            # Parsing best-effort
            text_preview = html[:1500].replace("<", " <").replace(">", "> ")
            return {
                "tipo": tipo_u,
                "numero": numero,
                "año": año,
                "citation_ref": f"{tipo_u}-{numero:03d}/{año}",
                "fuente_url": url,
                "exists": True,
                "html_excerpt": text_preview,
            }
        except Exception as e:
            # Si 404, retornar exists=false (no es error, es ausencia)
            return {
                "tipo": tipo_u,
                "numero": numero,
                "año": año,
                "citation_ref": f"{tipo_u}-{numero:03d}/{año}",
                "fuente_url": url,
                "exists": False,
                "error": str(e)[:200],
            }

    async def search_tema(self, *, tema: str, limit: int = 10) -> dict:
        """Búsqueda por tema en relatoría.

        Endpoint: https://www.corteconstitucional.gov.co/relatoria/buscador-new/?q={tema}
        """
        from urllib.parse import quote
        url = f"https://www.corteconstitucional.gov.co/relatoria/buscador-new/?q={quote(tema)}"
        try:
            html = await self._http_get(url)
            # Best-effort extraction; el portal tiene markup complejo
            count_estimated = html.count("class=\"resultado")
            return {
                "tema": tema,
                "fuente_url": url,
                "count_estimated": count_estimated,
                "note": "Para análisis detallado usar el portal directamente.",
            }
        except Exception as e:
            return {"tema": tema, "fuente_url": url, "error": str(e)[:200]}

    async def latest_sentencias(self, *, sala: str = "plena", limit: int = 10) -> dict:
        """Últimas sentencias publicadas por sala."""
        url = "https://www.corteconstitucional.gov.co/relatoria/"
        try:
            html = await self._http_get(url)
            return {
                "sala": sala,
                "fuente_url": url,
                "html_length": len(html),
                "note": "Listado parseable en relatoria/. Sugerir consulta directa para top items.",
            }
        except Exception as e:
            return {"sala": sala, "fuente_url": url, "error": str(e)[:200]}
