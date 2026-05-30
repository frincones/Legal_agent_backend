"""MCP Server · SUIN-Juriscol (normativa colombiana oficial)."""
from __future__ import annotations

from .base import BaseMCPClient


class SuinClient(BaseMCPClient):
    server_name = "suin"
    max_per_minute = 10
    default_cache_ttl_s = 7 * 86400   # 7 días (normas pueden cambiar)

    def _register_methods(self):
        return {
            "fetch_norma": self.fetch_norma,
            "search_articulo": self.search_articulo,
            "vigencia_check": self.vigencia_check,
        }

    async def fetch_norma(self, *, tipo: str, numero: int, año: int) -> dict:
        """Fetch norma (Ley, Decreto, Resolución) por número y año.

        URL base: https://www.suin-juriscol.gov.co/viewDocument.asp?id={...}
        El ID interno se busca via el buscador del portal.
        """
        from urllib.parse import quote
        tipo = (tipo or "ley").lower().strip()
        url_search = f"https://www.suin-juriscol.gov.co/buscador/?b={quote(f'{tipo} {numero} de {año}')}"
        try:
            html = await self._http_get(url_search)
            return {
                "tipo": tipo,
                "numero": numero,
                "año": año,
                "citation_ref": f"{tipo.upper()} {numero}/{año}",
                "fuente_url": url_search,
                "html_length": len(html),
                "note": "Para texto completo, navegar al primer resultado.",
            }
        except Exception as e:
            return {
                "tipo": tipo, "numero": numero, "año": año,
                "fuente_url": url_search, "error": str(e)[:200],
            }

    async def search_articulo(self, *, codigo: str, articulo: int) -> dict:
        """Búsqueda de artículo específico en un código (CGP, CC, CST, etc.).

        SUIN tiene URLs canónicas por código:
          - CC: https://www.suin-juriscol.gov.co/viewDocument.asp?id=1827111
          - CGP: https://www.secretariasenado.gov.co/senado/basedoc/ley_1564_2012.html (mejor source)
          - CCo: https://www.secretariasenado.gov.co/senado/basedoc/codigo_comercio.html
        Para los códigos clásicos, redirige al portal correcto.
        """
        codigo_u = (codigo or "").upper().strip()
        codigo_urls = {
            "CC": "https://www.suin-juriscol.gov.co/viewDocument.asp?id=1827111",
            "CGP": "https://www.secretariasenado.gov.co/senado/basedoc/ley_1564_2012.html",
            "CCo": "https://www.secretariasenado.gov.co/senado/basedoc/codigo_comercio.html",
            "CST": "https://www.secretariasenado.gov.co/senado/basedoc/codigo_sustantivo_trabajo.html",
            "CN": "https://www.secretariasenado.gov.co/senado/basedoc/constitucion_politica_1991.html",
            "CP": "https://www.secretariasenado.gov.co/senado/basedoc/ley_0599_2000.html",
            "CPP": "https://www.secretariasenado.gov.co/senado/basedoc/ley_0906_2004.html",
        }
        url = codigo_urls.get(codigo_u, "https://www.suin-juriscol.gov.co/")
        return {
            "codigo": codigo_u,
            "articulo": articulo,
            "fuente_url": url,
            "fuente_url_articulo": f"{url}#articulo{articulo}",
            "note": f"Art. {articulo} {codigo_u} consultable en URL canónica.",
        }

    async def vigencia_check(self, *, norma_id: str) -> dict:
        """Verifica vigencia de una norma específica.

        SUIN tiene un sistema interno de relaciones (deroga, modifica, etc.)
        accesible via viewRelations.asp.
        """
        url = f"https://www.suin-juriscol.gov.co/viewDocument.asp?id={norma_id}"
        try:
            html = await self._http_get(url)
            # Heurística: buscar markers de derogación en el HTML
            derogada_keywords = ["derogad", "derogad", "norma sin vigencia"]
            is_derogada = any(kw in html.lower() for kw in derogada_keywords)
            return {
                "norma_id": norma_id,
                "fuente_url": url,
                "vigente": not is_derogada,
                "derogada": is_derogada,
                "note": "Verificación heurística. Para análisis legal usar abogado.",
            }
        except Exception as e:
            return {
                "norma_id": norma_id, "fuente_url": url,
                "vigente": True,  # asume vigente si no podemos verificar
                "error": str(e)[:200],
            }
