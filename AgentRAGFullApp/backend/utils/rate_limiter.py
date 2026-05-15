"""Sprint 28 · Rate limiter · in-memory + per-key.

Implementación simple sliding-window contador en memoria del proceso.
NO requiere Redis. Para multi-worker, cada worker tiene su contador local
(esto es OK para protección anti-abuso básica, no para enforcement preciso).

API:
  - rate_limit(key, limit, window_seconds) → tuple[ok, remaining, reset_at]
  - check(request, scope='ip', limit=60, window=60) → raise 429 si excede
  - dependency factory para usar en routers

Uso:
    from utils.rate_limiter import rate_limit_dep

    router = APIRouter(
        prefix="/v1/foo",
        dependencies=[Depends(rate_limit_dep(limit=10, window_seconds=60))],
    )
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Optional

from fastapi import HTTPException, Request, Response, status

logger = logging.getLogger(__name__)


# In-memory store: { key: deque of timestamps }
_store: dict[str, deque[float]] = {}
_MAX_KEYS = 10_000  # evita memory leak indefinido


def _cleanup_if_needed():
    """Si pasamos del límite, removemos las más viejas."""
    if len(_store) > _MAX_KEYS:
        # Drop oldest 10%
        items = list(_store.items())
        items.sort(key=lambda kv: max(kv[1]) if kv[1] else 0)
        for k, _ in items[: _MAX_KEYS // 10]:
            _store.pop(k, None)


def rate_limit(key: str, limit: int, window_seconds: int) -> tuple[bool, int, float]:
    """Sliding-window check. Returns (ok, remaining, reset_at_unix).

    - ok=True: permitido, conteo registrado
    - ok=False: bloqueado (NO se registra)
    """
    now = time.time()
    window_start = now - window_seconds
    timestamps = _store.setdefault(key, deque())

    # Remove expired
    while timestamps and timestamps[0] < window_start:
        timestamps.popleft()

    if len(timestamps) >= limit:
        reset_at = timestamps[0] + window_seconds
        return False, 0, reset_at

    timestamps.append(now)
    _cleanup_if_needed()
    return True, limit - len(timestamps), now + window_seconds


def _key_from_request(request: Request, scope: str) -> str:
    if scope == "ip":
        ip = request.client.host if request.client else "unknown"
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            ip = forwarded.split(",")[0].strip()
        return f"ip:{ip}:{request.url.path}"
    if scope == "user":
        # Caller usa auth.user_id si lo tiene; fallback IP
        return f"unknown:{request.url.path}"
    return f"{scope}:{request.url.path}"


def rate_limit_dep(
    limit: int = 60,
    window_seconds: int = 60,
    scope: str = "ip",
):
    """Dependency factory.

    Args:
      limit: requests permitidos en la ventana
      window_seconds: tamaño ventana
      scope: 'ip' | 'user' (user requiere principal en kwargs)
    """
    async def _dep(request: Request, response: Response):
        key = _key_from_request(request, scope)
        ok, remaining, reset_at = rate_limit(key, limit, window_seconds)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(int(reset_at))
        if not ok:
            retry_after = max(1, int(reset_at - time.time()))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "rate_limit_exceeded",
                    "message": f"Demasiadas solicitudes · reintenta en {retry_after}s",
                    "retry_after_seconds": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )
        return True
    return _dep
