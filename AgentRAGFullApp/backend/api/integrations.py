"""Sprint A · Router unificado /v1/integrations.

Endpoints:
  GET    /v1/integrations                          · Lista firm_integrations
  POST   /v1/integrations/{provider}/start         · Inicia OAuth, retorna auth_url
  GET    /v1/integrations/oauth/callback           · Callback unificado de los 4 providers
  DELETE /v1/integrations/{id}                     · Revoca integración
  POST   /v1/integrations/{id}/test                · Ping conectividad

Providers soportados en esta tabla: google_drive, onedrive, dropbox, docusign.
Email (gmail/outlook) y Calendar (google/outlook) y WhatsApp NO van aquí:
mantienen sus tablas y endpoints especializados existentes.

Patrón:
  1. Frontend POST /start → backend genera state + PKCE → INSERT oauth_states
     → retorna { auth_url } al frontend
  2. Frontend redirige al usuario al auth_url
  3. Provider redirige a /oauth/callback?code=...&state=...
  4. Backend valida state, cambia code, cifra tokens, upsert firm_integrations
  5. Backend redirige a /auth/oauth-return?ok=<provider>
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse

from utils.auth import Principal, get_current_firm
from utils import crypto, oauth as oauth_utils

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/integrations", tags=["integrations"])

# Frontend base URL para el redirect post-callback
FRONTEND_BASE = os.getenv(
    "LEXAI_FRONTEND_BASE",
    "https://lexai-frontend-rho.vercel.app",
).rstrip("/")

NEW_PROVIDERS = {"google_drive", "onedrive", "dropbox", "docusign"}


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _frontend_redirect(path: str = "/settings/integraciones", **params) -> str:
    """Construye URL al frontend con query params."""
    from urllib.parse import urlencode
    query = urlencode({k: v for k, v in params.items() if v is not None})
    base = f"{FRONTEND_BASE}{path}"
    return f"{base}?{query}" if query else base


# ─────────────────────────────────────────────────────────────────────
# GET /v1/integrations · listar firm_integrations de la firma
# ─────────────────────────────────────────────────────────────────────

@router.get("")
async def list_integrations(
    principal: Principal = Depends(get_current_firm),
):
    """Lista todas las integraciones de la firma del usuario (tabla
    firm_integrations · NO incluye email/calendar/whatsapp legacy)."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return []
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, provider, account_id, account_label,
                   status, last_status, last_error, last_synced_at,
                   oauth_expires_at, scopes, active, metadata,
                   created_at, updated_at
              from firm_integrations
             where firm_id = $1::uuid
               and active = true
             order by created_at desc
            """,
            principal.firm_id,
        )
    result = []
    for r in rows:
        d = dict(r)
        # asyncpg datetime → ISO
        for k in ("oauth_expires_at", "last_synced_at", "created_at", "updated_at"):
            if d.get(k):
                d[k] = d[k].isoformat()
        # metadata jsonb quirk
        md = d.get("metadata")
        if isinstance(md, str):
            try:
                d["metadata"] = json.loads(md)
            except Exception:
                d["metadata"] = {}
        d["id"] = str(d["id"])
        result.append(d)
    return result


# ─────────────────────────────────────────────────────────────────────
# POST /v1/integrations/{provider}/start · iniciar OAuth
# ─────────────────────────────────────────────────────────────────────

@router.post("/{provider}/start")
async def start_oauth(
    provider: str,
    request: Request,
    principal: Principal = Depends(get_current_firm),
):
    """Genera state + PKCE, INSERT oauth_states, retorna auth_url."""
    if provider not in NEW_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail={"error": "unsupported_provider",
                    "message": f"Provider '{provider}' no es soportado por este endpoint. "
                               f"Usa /v1/email/oauth/* o /v1/calendar/oauth/* para gmail/outlook/google."},
        )
    if not oauth_utils.credentials_configured(provider):
        cfg = oauth_utils.get_provider_cfg(provider)
        raise HTTPException(
            status_code=503,
            detail={
                "error": "oauth_not_configured",
                "message": f"OAuth credentials no configuradas para {provider}. "
                           f"Configura {cfg.get('client_id_env')} y {cfg.get('client_secret_env')} en Railway.",
            },
        )

    from utils.db import get_storage
    storage = await get_storage()

    # PKCE (si el provider lo soporta)
    code_verifier = None
    code_challenge = None
    cfg = oauth_utils.get_provider_cfg(provider)
    if cfg.get("uses_pkce"):
        code_verifier, code_challenge = oauth_utils.generate_pkce()

    # Crear state
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    redirect_to = body.get("redirect_to") if isinstance(body, dict) else None

    state = await oauth_utils.create_oauth_state(
        storage.pool,
        firm_id=principal.firm_id,
        user_id=principal.user_id,
        provider=provider,
        code_verifier=code_verifier,
        redirect_to=redirect_to or "/settings/integraciones",
        metadata={"initiated_by": "integrations_router"},
    )

    auth_url = oauth_utils.build_auth_url(
        provider,
        state=state,
        code_challenge=code_challenge,
    )
    return {"auth_url": auth_url, "state": state, "provider": provider}


# ─────────────────────────────────────────────────────────────────────
# GET /v1/integrations/oauth/callback · callback unificado
# ─────────────────────────────────────────────────────────────────────

@router.get("/oauth/callback")
async def oauth_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
):
    """Callback OAuth unificado. Maneja los 4 providers nuevos.

    NO requiere auth · es invocado por el provider externo (Google,
    Microsoft, Dropbox, DocuSign). La validación se hace contra
    oauth_states (CSRF protection).
    """
    # Provider error
    if error:
        return RedirectResponse(
            _frontend_redirect("/auth/oauth-return",
                               error=error,
                               description=error_description),
            status_code=302,
        )
    if not code or not state:
        return RedirectResponse(
            _frontend_redirect("/auth/oauth-return", error="missing_code_or_state"),
            status_code=302,
        )

    from utils.db import get_storage
    storage = await get_storage()

    # Consume state (atomic SELECT + DELETE)
    state_row = await oauth_utils.consume_oauth_state(storage.pool, state)
    if not state_row:
        return RedirectResponse(
            _frontend_redirect("/auth/oauth-return", error="invalid_or_expired_state"),
            status_code=302,
        )

    provider = state_row["provider"]
    firm_id = state_row["firm_id"]
    user_id = state_row["user_id"]
    code_verifier = state_row["code_verifier"]
    redirect_to = state_row.get("redirect_to") or "/settings/integraciones"

    # Code exchange
    tokens = await oauth_utils.exchange_code(
        provider,
        code=code,
        code_verifier=code_verifier,
    )
    if not tokens or not tokens.get("access_token"):
        return RedirectResponse(
            _frontend_redirect("/auth/oauth-return",
                               provider=provider,
                               error="token_exchange_failed"),
            status_code=302,
        )

    # Cifrar tokens
    try:
        access_enc = crypto.encrypt(tokens["access_token"])
        refresh_enc = crypto.encrypt(tokens["refresh_token"]) if tokens.get("refresh_token") else None
        encryption_version = crypto.CURRENT_VERSION
    except RuntimeError as e:
        logger.error("encryption failed: %s", e)
        return RedirectResponse(
            _frontend_redirect("/auth/oauth-return",
                               provider=provider,
                               error="encryption_not_configured"),
            status_code=302,
        )

    # Calcular expiración
    expires_at = None
    if tokens.get("expires_in"):
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(tokens["expires_in"]))

    # Dispatch · ¿tabla nueva (firm_integrations) o legacy?
    cfg = oauth_utils.get_provider_cfg(provider)
    legacy_table = cfg.get("legacy_table")

    if legacy_table == "email_integrations":
        await _upsert_email_legacy(
            storage.pool, provider, firm_id, user_id, tokens,
            access_enc, refresh_enc, expires_at, encryption_version,
        )
    elif legacy_table == "calendar_integrations":
        await _upsert_calendar_legacy(
            storage.pool, provider, firm_id, user_id, tokens,
            access_enc, refresh_enc, expires_at, encryption_version,
        )
    elif provider in NEW_PROVIDERS:
        await _upsert_firm_integration(
            storage.pool, provider, firm_id, user_id, tokens,
            access_enc, refresh_enc, expires_at, encryption_version,
        )
    else:
        return RedirectResponse(
            _frontend_redirect("/auth/oauth-return",
                               provider=provider,
                               error="unsupported_provider"),
            status_code=302,
        )

    return RedirectResponse(
        _frontend_redirect("/auth/oauth-return", ok=provider),
        status_code=302,
    )


async def _upsert_firm_integration(
    pool, provider, firm_id, user_id, tokens,
    access_enc, refresh_enc, expires_at, encryption_version,
):
    mcp_server_url = None
    if provider == "docusign":
        mcp_server_url = "https://mcp-d.docusign.com/mcp"

    scopes_list = tokens.get("scope", "").split() if tokens.get("scope") else []
    async with pool.acquire() as conn:
        await conn.execute(
            """
            insert into firm_integrations
              (firm_id, user_id, provider, account_id, account_label,
               oauth_access_token_enc, oauth_refresh_token_enc, oauth_expires_at,
               encryption_version, scopes, mcp_server_url,
               status, last_synced_at, metadata)
            values ($1::uuid, $2::uuid, $3, $4, $5,
                    $6, $7, $8, $9, $10, $11,
                    'connected', now(), $12::jsonb)
            on conflict (firm_id, provider, account_id) do update
              set oauth_access_token_enc = excluded.oauth_access_token_enc,
                  oauth_refresh_token_enc = coalesce(excluded.oauth_refresh_token_enc,
                                                     firm_integrations.oauth_refresh_token_enc),
                  oauth_expires_at = excluded.oauth_expires_at,
                  encryption_version = excluded.encryption_version,
                  scopes = excluded.scopes,
                  account_label = excluded.account_label,
                  mcp_server_url = excluded.mcp_server_url,
                  status = 'connected',
                  last_status = null,
                  last_error = null,
                  last_synced_at = now(),
                  active = true,
                  updated_at = now()
            """,
            str(firm_id), str(user_id), provider,
            tokens.get("account_id"), tokens.get("email"),
            access_enc, refresh_enc, expires_at,
            encryption_version, scopes_list, mcp_server_url,
            json.dumps({"token_type": tokens.get("token_type", "Bearer")}),
        )


async def _upsert_email_legacy(
    pool, provider, firm_id, user_id, tokens,
    access_enc, refresh_enc, expires_at, encryption_version,
):
    """Compat-safe upsert para email_integrations (gmail/outlook).
    Mantiene el contrato sprint 5/6."""
    scopes_list = tokens.get("scope", "").split() if tokens.get("scope") else []
    async with pool.acquire() as conn:
        # ¿Existe ya integration con mismo email? Si sí, UPDATE; si no, INSERT
        existing = await conn.fetchrow(
            """
            select id from email_integrations
             where firm_id = $1::uuid and provider = $2
               and email_address = $3
               and active = true
             limit 1
            """,
            str(firm_id), provider, tokens.get("email"),
        )
        if existing:
            await conn.execute(
                """
                update email_integrations
                   set oauth_access_token_enc = $1,
                       oauth_refresh_token_enc = coalesce($2, oauth_refresh_token_enc),
                       oauth_expires_at = $3,
                       encryption_version = $4,
                       scopes = $5,
                       status = 'connected',
                       last_status = null,
                       last_error = null,
                       updated_at = now()
                 where id = $6
                """,
                access_enc, refresh_enc, expires_at, encryption_version,
                scopes_list, existing["id"],
            )
        else:
            await conn.execute(
                """
                insert into email_integrations
                  (firm_id, user_id, provider, email_address,
                   oauth_access_token_enc, oauth_refresh_token_enc,
                   oauth_expires_at, encryption_version, scopes, status)
                values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9, 'connected')
                """,
                str(firm_id), str(user_id), provider, tokens.get("email"),
                access_enc, refresh_enc, expires_at, encryption_version, scopes_list,
            )


async def _upsert_calendar_legacy(
    pool, provider, firm_id, user_id, tokens,
    access_enc, refresh_enc, expires_at, encryption_version,
):
    """Compat-safe upsert para calendar_integrations (google/outlook)."""
    scopes_list = tokens.get("scope", "").split() if tokens.get("scope") else []
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            """
            select id from calendar_integrations
             where firm_id = $1::uuid and provider = $2
               and email_address = $3
               and active = true
             limit 1
            """,
            str(firm_id), provider, tokens.get("email"),
        )
        if existing:
            await conn.execute(
                """
                update calendar_integrations
                   set oauth_access_token_enc = $1,
                       oauth_refresh_token_enc = coalesce($2, oauth_refresh_token_enc),
                       oauth_expires_at = $3,
                       encryption_version = $4,
                       scopes = $5,
                       status = 'connected',
                       last_status = null,
                       last_error = null,
                       updated_at = now()
                 where id = $6
                """,
                access_enc, refresh_enc, expires_at, encryption_version,
                scopes_list, existing["id"],
            )
        else:
            await conn.execute(
                """
                insert into calendar_integrations
                  (firm_id, user_id, provider, email_address,
                   oauth_access_token_enc, oauth_refresh_token_enc,
                   oauth_expires_at, encryption_version, scopes, status)
                values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9, 'connected')
                """,
                str(firm_id), str(user_id), provider, tokens.get("email"),
                access_enc, refresh_enc, expires_at, encryption_version, scopes_list,
            )


# ─────────────────────────────────────────────────────────────────────
# DELETE /v1/integrations/{id} · revocar
# ─────────────────────────────────────────────────────────────────────

@router.delete("/{integration_id}")
async def delete_integration(
    integration_id: UUID,
    principal: Principal = Depends(get_current_firm),
):
    """Soft delete · marca active=false + status='revoked'."""
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update firm_integrations
               set active = false,
                   status = 'revoked',
                   updated_at = now()
             where id = $1::uuid and firm_id = $2::uuid
            returning id, provider
            """,
            str(integration_id), principal.firm_id,
        )
    if not row:
        raise HTTPException(404, "Integration not found")
    return {"ok": True, "id": str(row["id"]), "provider": row["provider"]}


# ─────────────────────────────────────────────────────────────────────
# POST /v1/integrations/{id}/test · ping conectividad
# ─────────────────────────────────────────────────────────────────────

@router.post("/{integration_id}/test")
async def test_integration(
    integration_id: UUID,
    principal: Principal = Depends(get_current_firm),
):
    """Verifica que el access_token funciona haciendo un userinfo lookup."""
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, provider, oauth_access_token_enc, oauth_refresh_token_enc,
                   oauth_expires_at
              from firm_integrations
             where id = $1::uuid and firm_id = $2::uuid and active = true
            """,
            str(integration_id), principal.firm_id,
        )
    if not row:
        raise HTTPException(404, "Integration not found")

    access_token = crypto.decrypt(row["oauth_access_token_enc"])
    if not access_token:
        raise HTTPException(500, "Failed to decrypt access token")

    provider = row["provider"]
    # Llama al userinfo de cada provider para verificar el token
    if provider == "google_drive":
        userinfo = await oauth_utils._fetch_google_userinfo(access_token)
    elif provider == "onedrive":
        userinfo = await oauth_utils._fetch_microsoft_userinfo(access_token)
    elif provider == "dropbox":
        userinfo = await oauth_utils._fetch_dropbox_userinfo(access_token)
    elif provider == "docusign":
        userinfo = await oauth_utils._fetch_docusign_userinfo(access_token)
    else:
        userinfo = None

    if not userinfo:
        async with storage.pool.acquire() as conn:
            await conn.execute(
                """
                update firm_integrations
                   set status = 'error', last_error = 'token_test_failed',
                       updated_at = now()
                 where id = $1::uuid
                """,
                str(integration_id),
            )
        return {"ok": False, "error": "token_test_failed", "provider": provider}

    return {"ok": True, "provider": provider, "account": userinfo}
