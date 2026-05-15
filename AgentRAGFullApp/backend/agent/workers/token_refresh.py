"""Sprint A · Worker · refresh OAuth tokens próximos a expirar.

Patrón:
  - Llama cada 5 minutos (pg_cron + pg_net.http_post o Railway cron)
  - Lee firm_integrations + calendar_integrations + email_integrations
    con oauth_expires_at entre now() y now() + 10min
  - Por cada uno: llama refresh_access_token() con el refresh_token
  - Actualiza access_token_enc + expires_at + last_synced_at

Idempotente: si refresh falla, marca status='expired' pero no rompe nada.

Llamado desde:
  - endpoint admin /v1/admin/refresh-tokens (admin-only)
  - pg_cron job 'token_refresh_due' (sprint B)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from utils import crypto
from utils.oauth import refresh_access_token

logger = logging.getLogger(__name__)


async def _refresh_one_row(
    pool,
    *,
    table: str,
    row: dict[str, Any],
) -> dict[str, Any]:
    """Refresca el token de una fila individual."""
    provider = row["provider"]
    refresh_enc = row.get("oauth_refresh_token_enc")
    if not refresh_enc:
        return {"id": str(row["id"]), "provider": provider, "skipped": "no_refresh_token"}

    refresh_token = crypto.decrypt(refresh_enc)
    if not refresh_token:
        return {"id": str(row["id"]), "provider": provider, "skipped": "decrypt_failed"}

    tokens = await refresh_access_token(provider, refresh_token=refresh_token)
    if not tokens or not tokens.get("access_token"):
        async with pool.acquire() as conn:
            await conn.execute(
                f"""
                update {table}
                   set status = 'expired',
                       last_status = 'refresh_failed',
                       updated_at = now()
                 where id = $1
                """,
                row["id"],
            )
        return {"id": str(row["id"]), "provider": provider, "refreshed": False}

    new_access_enc = crypto.encrypt(tokens["access_token"])
    new_refresh_enc = (
        crypto.encrypt(tokens["refresh_token"])
        if tokens.get("refresh_token") and tokens["refresh_token"] != refresh_token
        else None
    )
    expires_at = None
    if tokens.get("expires_in"):
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(tokens["expires_in"]))

    async with pool.acquire() as conn:
        if new_refresh_enc is not None:
            await conn.execute(
                f"""
                update {table}
                   set oauth_access_token_enc = $1,
                       oauth_refresh_token_enc = $2,
                       oauth_expires_at = $3,
                       status = 'connected',
                       last_status = null,
                       last_error = null,
                       updated_at = now()
                 where id = $4
                """,
                new_access_enc, new_refresh_enc, expires_at, row["id"],
            )
        else:
            await conn.execute(
                f"""
                update {table}
                   set oauth_access_token_enc = $1,
                       oauth_expires_at = $2,
                       status = 'connected',
                       last_status = null,
                       last_error = null,
                       updated_at = now()
                 where id = $3
                """,
                new_access_enc, expires_at, row["id"],
            )
    return {"id": str(row["id"]), "provider": provider, "refreshed": True}


async def refresh_due_tokens(pool, window_minutes: int = 10) -> dict[str, Any]:
    """Refresca todos los tokens que expiran en los próximos `window_minutes`."""
    stats = {"firm_integrations": [], "calendar_integrations": [], "email_integrations": []}

    # firm_integrations (providers nuevos)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, provider, oauth_refresh_token_enc
              from firm_integrations
             where active = true
               and oauth_refresh_token_enc is not null
               and oauth_expires_at is not null
               and oauth_expires_at between now() and now() + ($1 || ' minutes')::interval
            """,
            str(window_minutes),
        )
    for r in rows:
        result = await _refresh_one_row(pool, table="firm_integrations", row=dict(r))
        stats["firm_integrations"].append(result)

    # calendar_integrations (provider google/outlook)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, provider, oauth_refresh_token_enc
              from calendar_integrations
             where active = true
               and oauth_refresh_token_enc is not null
               and oauth_expires_at is not null
               and oauth_expires_at between now() and now() + ($1 || ' minutes')::interval
            """,
            str(window_minutes),
        )
    for r in rows:
        result = await _refresh_one_row(pool, table="calendar_integrations", row=dict(r))
        stats["calendar_integrations"].append(result)

    # email_integrations (provider gmail/outlook)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, provider, oauth_refresh_token_enc
              from email_integrations
             where active = true
               and oauth_refresh_token_enc is not null
               and oauth_expires_at is not null
               and oauth_expires_at between now() and now() + ($1 || ' minutes')::interval
            """,
            str(window_minutes),
        )
    for r in rows:
        result = await _refresh_one_row(pool, table="email_integrations", row=dict(r))
        stats["email_integrations"].append(result)

    return stats
