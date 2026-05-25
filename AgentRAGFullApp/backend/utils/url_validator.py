"""Sprint M17 · HEAD validation con cache para URLs de citas."""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

HEAD_TIMEOUT = httpx.Timeout(5.0, connect=3.0)
CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 días


async def validate_url_responsive(url: str, pool=None) -> Tuple[bool, Optional[int]]:
    """Valida que la URL responda HTTP 200/3xx via HEAD request.

    Returns:
        (is_valid: bool, http_status: int | None)

    Cache hits evitan HEAD repetidos a la misma URL (TTL 7d).
    """
    if not url:
        return False, None

    # v2 prefix: invalida cache viejo cuando se cambia la lista de candidatos
    cache_key = f"head:v2:{hashlib.sha256(url.encode()).hexdigest()[:32]}"

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

    # HEAD request
    is_valid = False
    status = None
    try:
        async with httpx.AsyncClient(
            timeout=HEAD_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "LexAI-CitationValidator/1.0"},
        ) as client:
            resp = await client.head(url)
            status = resp.status_code
            # Aceptar 200 y redirects (3xx). 404/5xx son inválidos.
            is_valid = 200 <= status < 400
    except httpx.HTTPError as e:
        logger.debug("HEAD %s failed: %s", url, e)
        is_valid = False
    except Exception as e:
        logger.warning("HEAD %s unexpected: %s", url, e)
        is_valid = False

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
