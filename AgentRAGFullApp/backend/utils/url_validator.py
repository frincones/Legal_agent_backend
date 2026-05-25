"""Sprint M17 · URL validation con detección de soft 404 + cache."""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# GET en lugar de HEAD porque algunos sitios (funcionpublica.gov.co) redirigen
# a páginas de error con HTTP 200 (soft 404) que HEAD no detecta.
GET_TIMEOUT = httpx.Timeout(10.0, connect=4.0)
CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 días

# Markers que indican que el contenido es una página de error, no la norma real.
# Si la URL final o el body contienen alguno, tratamos como inválido.
_SOFT_404_URL_MARKERS = (
    "norma_error.php",
    "not_found",
    "/error/",
    "/404",
)
_SOFT_404_BODY_MARKERS = (
    "esta página no está disponible",
    "esta pagina no esta disponible",
    "la norma puede que ya no este disponible",
    "enlace erróneo",
    "enlace erroneo",
    "página no encontrada",
    "pagina no encontrada",
    "page not found",
)
# Tamaño mínimo de body real (las páginas de error de FunPub pesan ~13KB)
_MIN_BODY_BYTES = 18000


def _is_soft_404(final_url: str, body: str) -> bool:
    """Detecta páginas que retornan 200 pero son error en realidad."""
    fu = final_url.lower() if final_url else ""
    if any(m in fu for m in _SOFT_404_URL_MARKERS):
        return True
    if body:
        bl = body[:5000].lower()
        if any(m in bl for m in _SOFT_404_BODY_MARKERS):
            return True
    return False


async def validate_url_responsive(url: str, pool=None) -> Tuple[bool, Optional[int]]:
    """Valida que la URL responda HTTP 200 con contenido real.

    Usa GET (no HEAD) para inspeccionar redirect final + body y detectar
    soft 404 (páginas de error con HTTP 200).

    Returns:
        (is_valid: bool, http_status: int | None)
    """
    if not url:
        return False, None

    # v3 prefix: invalida cache de HEAD (v2) ahora que usamos GET + soft 404
    cache_key = f"head:v3:{hashlib.sha256(url.encode()).hexdigest()[:32]}"

    # Cache lookup
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT content_jsonb FROM external_fetch_cache
                    WHERE cache_key = $1
                      AND fetched_at + (ttl_seconds || ' seconds')::interval > now()
                    """,
                    cache_key,
                )
            if row and row["content_jsonb"]:
                d = row["content_jsonb"]
                if isinstance(d, str):
                    import json as _json
                    d = _json.loads(d)
                return bool(d.get("valid", False)), d.get("status")
        except Exception as e:
            logger.warning("url_validator cache lookup failed: %s", e)

    # GET request con detección de soft 404
    is_valid = False
    status: Optional[int] = None
    try:
        async with httpx.AsyncClient(
            timeout=GET_TIMEOUT,
            follow_redirects=True,
            headers={
                # User-Agent realista; algunos sitios bloquean bots
                "User-Agent": "Mozilla/5.0 (compatible; LexAI-CitationValidator/1.0)",
                "Accept": "text/html,application/xhtml+xml",
            },
        ) as client:
            resp = await client.get(url)
            status = resp.status_code
            if 200 <= status < 400:
                final_url = str(resp.url)
                body = resp.text or ""
                # Rechazar soft 404 + bodies sospechosamente pequeños (páginas de error de FunPub ~13KB)
                if _is_soft_404(final_url, body):
                    is_valid = False
                    logger.debug("soft_404 detected: %s -> %s", url, final_url)
                elif len(body) < _MIN_BODY_BYTES:
                    is_valid = False
                    logger.debug("body too small (%d bytes) for %s", len(body), url)
                else:
                    is_valid = True
    except httpx.HTTPError as e:
        logger.debug("GET %s failed: %s", url, e)
    except Exception as e:
        logger.warning("GET %s unexpected: %s", url, e)

    # Cache result
    if pool is not None:
        try:
            import json as _json
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO external_fetch_cache
                      (cache_key, source, content_jsonb, status, ttl_seconds)
                    VALUES ($1, 'url_validator', $2::jsonb, 'ok', $3)
                    ON CONFLICT (cache_key) DO UPDATE
                      SET content_jsonb = EXCLUDED.content_jsonb,
                          fetched_at = now(),
                          ttl_seconds = EXCLUDED.ttl_seconds
                    """,
                    cache_key,
                    _json.dumps({"valid": is_valid, "status": status, "url": url[:300]}),
                    CACHE_TTL_SECONDS,
                )
        except Exception as e:
            logger.warning("url_validator cache_set failed: %s", e)

    return is_valid, status


async def find_valid_url(
    candidates: list[str],
    pool=None,
) -> Tuple[Optional[str], Optional[int]]:
    """Prueba múltiples URLs candidatas y retorna la primera que responde.

    Útil cuando hay variantes de URL (ej. SU449-20 vs SU-449-20).
    """
    for url in candidates:
        valid, status = await validate_url_responsive(url, pool)
        if valid:
            return url, status
    return None, None
