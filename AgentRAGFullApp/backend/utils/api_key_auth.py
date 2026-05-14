"""Sprint 14 · API key auth dependency para FastAPI.

Acepta tres modos:
  1. Header `X-API-Key: lex_live_...`
  2. Header `Authorization: Bearer lex_live_...`
  3. Query param `api_key=lex_live_...` (debugging únicamente)

Devuelve un Principal-compatible object con firm_id + scopes para reusar
en los endpoints públicos.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import Header, HTTPException, Query, Request

from utils.api_keys import hash_key, scope_allows

logger = logging.getLogger(__name__)


@dataclass
class ApiKeyPrincipal:
    firm_id: str
    api_key_id: str
    scopes: list[str]
    rate_limit_per_min: int


def _extract_key(
    x_api_key: Optional[str],
    authorization: Optional[str],
    api_key_query: Optional[str],
) -> Optional[str]:
    if x_api_key and x_api_key.startswith("lex_live_"):
        return x_api_key.strip()
    if authorization:
        a = authorization.strip()
        if a.lower().startswith("bearer "):
            tok = a[7:].strip()
            if tok.startswith("lex_live_"):
                return tok
    if api_key_query and api_key_query.startswith("lex_live_"):
        return api_key_query.strip()
    return None


async def require_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    authorization: Optional[str] = Header(default=None),
    api_key_query: Optional[str] = Query(default=None, alias="api_key"),
) -> ApiKeyPrincipal:
    plain = _extract_key(x_api_key, authorization, api_key_query)
    if not plain:
        raise HTTPException(401, "API key requerida (header X-API-Key o Bearer)")
    hashed = hash_key(plain)
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    started = time.time()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, firm_id, scopes, rate_limit_per_min, active,
                   expires_at, revoked_at
              from api_keys
             where key_hash = $1
            """,
            hashed,
        )
        if not row:
            raise HTTPException(401, "API key inválida")
        if not row["active"] or row["revoked_at"]:
            raise HTTPException(401, "API key revocada")
        if row["expires_at"] and row["expires_at"].timestamp() < time.time():
            raise HTTPException(401, "API key expirada")

        # Rate limiting: contamos last 60s en api_key_usage_log
        recent = await conn.fetchval(
            """
            select count(*) from api_key_usage_log
             where api_key_id = $1::uuid
               and occurred_at >= now() - interval '60 seconds'
            """,
            row["id"],
        )
        if (recent or 0) >= int(row["rate_limit_per_min"] or 60):
            raise HTTPException(
                429,
                f"Rate limit excedido ({row['rate_limit_per_min']}/min)",
            )

        # Update last_used + use_count
        ip = request.client.host if request.client else None
        xff = request.headers.get("x-forwarded-for")
        if xff:
            ip = xff.split(",")[0].strip()
        await conn.execute(
            """
            update api_keys set
              last_used_at = now(),
              last_used_ip = $2::inet,
              use_count = use_count + 1
             where id = $1::uuid
            """,
            row["id"], ip,
        )

    principal = ApiKeyPrincipal(
        firm_id=str(row["firm_id"]),
        api_key_id=str(row["id"]),
        scopes=list(row["scopes"] or []),
        rate_limit_per_min=int(row["rate_limit_per_min"] or 60),
    )

    # Audit log fire-and-forget al final del request
    request.state._api_key_started = started
    request.state._api_key_principal = principal
    return principal


async def log_api_call(request: Request, status_code: int) -> None:
    """Helper para logear el request. Idealmente llamado desde middleware."""
    principal: Optional[ApiKeyPrincipal] = getattr(request.state, "_api_key_principal", None)
    if not principal:
        return
    started: float = getattr(request.state, "_api_key_started", time.time())
    ms = int((time.time() - started) * 1000)
    ip = request.client.host if request.client else None
    xff = request.headers.get("x-forwarded-for")
    if xff:
        ip = xff.split(",")[0].strip()
    ua = request.headers.get("user-agent")
    try:
        from utils.db import get_storage
        storage = await get_storage()
        if not hasattr(storage, "pool"):
            return
        async with storage.pool.acquire() as conn:
            await conn.execute(
                """
                insert into api_key_usage_log
                  (firm_id, api_key_id, endpoint, method, status_code,
                   duration_ms, ip_address, user_agent)
                values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7::inet, $8)
                """,
                principal.firm_id, principal.api_key_id,
                str(request.url.path), request.method, status_code, ms, ip, ua,
            )
    except Exception as e:
        logger.debug("api_key audit log skipped: %s", e)


def require_scope(scope: str):
    """Factory para crear dependency con scope check."""
    async def _check(principal: ApiKeyPrincipal = None):  # type: ignore
        # FastAPI injection se hace cuando se usa con Depends en chain
        if not principal:
            raise HTTPException(401, "API key requerida")
        if not scope_allows(principal.scopes, scope):
            raise HTTPException(403, f"Scope insuficiente. Requiere: {scope}")
        return principal
    return _check
