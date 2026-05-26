"""Sprint M18 · Brave Search API wrapper.

Cliente minimal para Brave Search restringido a dominios oficiales gov.co.
Usado por SmartSearchTool para descubrir URLs canónicas de normas no
mapeadas en patterns hardcoded.

API docs: https://brave.com/search/api/
Endpoint: https://api.search.brave.com/res/v1/web/search

Quirks importantes (verificados manualmente):
- country=CO NO existe. Usar country=ALL o omitir.
- Restringir a gov.co se hace con operador en query: f'... site:gov.co'
- Tier validado: 50 req/sec, sin cap mensual visible.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
BRAVE_TIMEOUT = httpx.Timeout(8.0, connect=4.0)


@dataclass
class BraveResult:
    """Un resultado de búsqueda Brave."""
    url: str
    title: str
    description: str  # snippet
    rank: int


@dataclass
class BraveSearchResponse:
    """Respuesta normalizada de Brave."""
    query: str
    results: list[BraveResult] = field(default_factory=list)
    total: int = 0
    error: Optional[str] = None
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None and len(self.results) > 0


def _get_api_key() -> Optional[str]:
    """Lee la API key del entorno. Retorna None si no está configurada."""
    return os.getenv("BRAVE_SEARCH_API_KEY")


async def search(
    query: str,
    count: int = 5,
    restrict_govco: bool = True,
    client: Optional[httpx.AsyncClient] = None,
) -> BraveSearchResponse:
    """Ejecuta búsqueda en Brave Search.

    Args:
        query: query original (ej. "Ley 100 de 1993")
        count: número de resultados (max 20 por API)
        restrict_govco: añade `site:gov.co` al query
        client: httpx client reusable (opcional)

    Returns:
        BraveSearchResponse con results normalizados.
    """
    started = time.time()
    api_key = _get_api_key()
    if not api_key:
        return BraveSearchResponse(
            query=query,
            error="BRAVE_SEARCH_API_KEY not configured",
            duration_ms=int((time.time() - started) * 1000),
        )

    effective_query = query.strip()
    if restrict_govco and "site:" not in effective_query.lower():
        effective_query = f'{effective_query} site:gov.co'

    headers = {
        "X-Subscription-Token": api_key,
        "Accept": "application/json",
    }
    params = {
        "q": effective_query,
        "count": min(max(count, 1), 20),
        "country": "ALL",   # CO no soportado por Brave
        "safesearch": "off",
    }

    own_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=BRAVE_TIMEOUT)
        own_client = True

    try:
        resp = await client.get(BRAVE_ENDPOINT, headers=headers, params=params)
        if resp.status_code == 429:
            return BraveSearchResponse(
                query=effective_query,
                error="rate_limited",
                duration_ms=int((time.time() - started) * 1000),
            )
        if resp.status_code == 401:
            return BraveSearchResponse(
                query=effective_query,
                error="unauthorized_check_api_key",
                duration_ms=int((time.time() - started) * 1000),
            )
        if resp.status_code != 200:
            return BraveSearchResponse(
                query=effective_query,
                error=f"http_{resp.status_code}",
                duration_ms=int((time.time() - started) * 1000),
            )

        data = resp.json()
        raw_results = (data.get("web") or {}).get("results") or []
        results: list[BraveResult] = []
        for idx, r in enumerate(raw_results, start=1):
            url = r.get("url") or ""
            if not url:
                continue
            results.append(
                BraveResult(
                    url=url,
                    title=(r.get("title") or "")[:300],
                    description=(r.get("description") or "")[:500],
                    rank=idx,
                )
            )

        return BraveSearchResponse(
            query=effective_query,
            results=results,
            total=len(results),
            duration_ms=int((time.time() - started) * 1000),
        )
    except httpx.TimeoutException:
        return BraveSearchResponse(
            query=effective_query,
            error="timeout",
            duration_ms=int((time.time() - started) * 1000),
        )
    except httpx.HTTPError as e:
        logger.warning("Brave HTTP error: %s", e)
        return BraveSearchResponse(
            query=effective_query,
            error=f"http_error:{type(e).__name__}",
            duration_ms=int((time.time() - started) * 1000),
        )
    except Exception as e:
        logger.warning("Brave unexpected error: %s", e)
        return BraveSearchResponse(
            query=effective_query,
            error=f"exc:{type(e).__name__}",
            duration_ms=int((time.time() - started) * 1000),
        )
    finally:
        if own_client:
            await client.aclose()


def filter_govco(results: list[BraveResult]) -> list[BraveResult]:
    """Filtra solo URLs de dominios .gov.co."""
    out = []
    for r in results:
        url_lower = r.url.lower()
        if ".gov.co" in url_lower or "//gov.co" in url_lower:
            out.append(r)
    return out


def is_available() -> bool:
    """¿Hay API key configurada? Útil para skip gracioso en tests."""
    return _get_api_key() is not None
