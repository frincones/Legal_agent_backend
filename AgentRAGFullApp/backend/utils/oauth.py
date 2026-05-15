"""Sprint A · OAuth helpers unificados para integraciones colaborativas.

Provee:
  - generate_pkce()            · code_verifier + code_challenge (S256)
  - create_oauth_state()       · INSERT en oauth_states, retorna state + verifier
  - consume_oauth_state()      · SELECT + DELETE atómico, valida TTL + provider
  - exchange_code()            · code → tokens por provider (google/microsoft/dropbox/docusign)
  - build_auth_url()           · construye URL OAuth con PKCE por provider
  - PROVIDER_CONFIG            · scopes + endpoints por provider

Usado por backend/api/integrations.py para todos los providers nuevos
(google_drive, onedrive, dropbox, docusign), y por refactor compat-safe
de email_integrations.py y calendar_integrations.py.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
from typing import Any, Optional
from uuid import UUID
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
# PKCE
# ─────────────────────────────────────────────────────────────────────

def generate_pkce() -> tuple[str, str]:
    """Genera (code_verifier, code_challenge_S256).

    code_verifier: 64 bytes urlsafe-base64 (~86 chars).
    code_challenge: SHA256(verifier) en base64 urlsafe sin padding.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def generate_state() -> str:
    """Genera un state (nonce) de 32 bytes urlsafe (~43 chars)."""
    return secrets.token_urlsafe(32)


# ─────────────────────────────────────────────────────────────────────
# State storage (Postgres `oauth_states` table · sprint A migración)
# ─────────────────────────────────────────────────────────────────────

async def create_oauth_state(
    pool,
    *,
    firm_id: str | UUID,
    user_id: str | UUID,
    provider: str,
    code_verifier: Optional[str] = None,
    redirect_to: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> str:
    """INSERT en oauth_states. Retorna el state generado."""
    state = generate_state()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            insert into oauth_states (state, firm_id, user_id, provider,
                                       code_verifier, redirect_to, metadata)
            values ($1, $2::uuid, $3::uuid, $4, $5, $6, $7::jsonb)
            """,
            state, str(firm_id), str(user_id), provider, code_verifier,
            redirect_to or "/settings/integraciones",
            json.dumps(metadata or {}),
        )
    return state


async def consume_oauth_state(pool, state: str) -> Optional[dict[str, Any]]:
    """SELECT + DELETE atómico. Valida TTL (expires_at > now()).

    Retorna dict con keys: firm_id, user_id, provider, code_verifier,
    redirect_to, metadata. Si no existe o expiró, retorna None.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            delete from oauth_states
             where state = $1
               and expires_at > now()
            returning firm_id, user_id, provider, code_verifier,
                      redirect_to, metadata
            """,
            state,
        )
    if not row:
        return None
    result = dict(row)
    # metadata podría venir como str (asyncpg jsonb quirk) o dict
    md = result.get("metadata")
    if isinstance(md, str):
        try:
            result["metadata"] = json.loads(md)
        except Exception:
            result["metadata"] = {}
    return result


# ─────────────────────────────────────────────────────────────────────
# Provider configuration · auth + token endpoints, scopes
# ─────────────────────────────────────────────────────────────────────

PROVIDER_CONFIG: dict[str, dict[str, Any]] = {
    # --- DOCUMENTOS ---
    "google_drive": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "client_id_env": "GOOGLE_OAUTH_CLIENT_ID",
        "client_secret_env": "GOOGLE_OAUTH_CLIENT_SECRET",
        "default_scopes": [
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/drive.metadata.readonly",
            "https://www.googleapis.com/auth/userinfo.email",
            "openid",
        ],
        "extra_auth_params": {"access_type": "offline", "prompt": "consent"},
        "uses_pkce": True,
    },
    "onedrive": {
        "auth_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        "client_id_env": "MICROSOFT_OAUTH_CLIENT_ID",
        "client_secret_env": "MICROSOFT_OAUTH_CLIENT_SECRET",
        "tenant_env": "MICROSOFT_OAUTH_TENANT",  # default 'common'
        "default_scopes": [
            "Files.ReadWrite",
            "User.Read",
            "offline_access",
        ],
        "extra_auth_params": {"response_mode": "query"},
        "uses_pkce": True,
    },
    "dropbox": {
        "auth_url": "https://www.dropbox.com/oauth2/authorize",
        "token_url": "https://api.dropboxapi.com/oauth2/token",
        "client_id_env": "DROPBOX_CLIENT_ID",
        "client_secret_env": "DROPBOX_CLIENT_SECRET",
        "default_scopes": [
            "files.metadata.read",
            "files.content.read",
            "files.content.write",
            "account_info.read",
        ],
        "extra_auth_params": {"token_access_type": "offline"},
        "uses_pkce": True,
    },
    # --- FIRMA ---
    "docusign": {
        # Modo demo (developer account). Para producción cambiar a account.docusign.com
        "auth_url_env": "DOCUSIGN_ACCOUNT_BASE",   # ej. https://account-d.docusign.com
        "auth_path": "/oauth/auth",
        "token_path": "/oauth/token",
        "client_id_env": "DOCUSIGN_INTEGRATION_KEY",
        "client_secret_env": "DOCUSIGN_SECRET_KEY",
        "default_scopes": ["signature", "impersonation"],
        "extra_auth_params": {},
        "uses_pkce": False,  # DocuSign Auth Code Grant clásico
    },
    # --- LEGACY (refactor compat-safe sprint A) ---
    "gmail": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "client_id_env": "GMAIL_OAUTH_CLIENT_ID",
        "client_secret_env": "GMAIL_OAUTH_CLIENT_SECRET",
        "default_scopes": [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/userinfo.email",
        ],
        "extra_auth_params": {"access_type": "offline", "prompt": "consent"},
        "uses_pkce": True,
        "legacy_table": "email_integrations",
    },
    "outlook": {
        "auth_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        "client_id_env": "OUTLOOK_OAUTH_CLIENT_ID",
        "client_secret_env": "OUTLOOK_OAUTH_CLIENT_SECRET",
        "tenant_env": "OUTLOOK_OAUTH_TENANT",
        "default_scopes": [
            "Mail.Read",
            "Mail.Send",
            "User.Read",
            "offline_access",
        ],
        "extra_auth_params": {"response_mode": "query"},
        "uses_pkce": True,
        "legacy_table": "email_integrations",
    },
    "google": {
        # Para calendar_integrations · provider='google'
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "client_id_env": "GOOGLE_OAUTH_CLIENT_ID",
        "client_secret_env": "GOOGLE_OAUTH_CLIENT_SECRET",
        "default_scopes": [
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/userinfo.email",
        ],
        "extra_auth_params": {"access_type": "offline", "prompt": "consent"},
        "uses_pkce": True,
        "legacy_table": "calendar_integrations",
    },
}


def _resolve_url(template: str, provider: str) -> str:
    """Reemplaza {tenant} en URLs Microsoft."""
    if "{tenant}" in template:
        cfg = PROVIDER_CONFIG.get(provider, {})
        tenant_env = cfg.get("tenant_env", "")
        tenant = os.getenv(tenant_env, "common") if tenant_env else "common"
        template = template.replace("{tenant}", tenant)
    return template


def get_provider_cfg(provider: str) -> dict[str, Any]:
    """Retorna config validada del provider o {} si no existe."""
    return PROVIDER_CONFIG.get(provider, {})


def credentials_configured(provider: str) -> bool:
    """¿Hay client_id/client_secret en env vars para este provider?"""
    cfg = get_provider_cfg(provider)
    if not cfg:
        return False
    client_id = os.getenv(cfg.get("client_id_env", ""))
    client_secret = os.getenv(cfg.get("client_secret_env", ""))
    return bool(client_id and client_secret)


def get_redirect_uri() -> str:
    """URL de callback unificado · backend Railway."""
    base = os.getenv(
        "RAILWAY_PUBLIC_BASE_URL",
        "https://legal-agent-backend-production-fcfa.up.railway.app",
    ).rstrip("/")
    return f"{base}/v1/integrations/oauth/callback"


# ─────────────────────────────────────────────────────────────────────
# Build auth URL
# ─────────────────────────────────────────────────────────────────────

def build_auth_url(
    provider: str,
    *,
    state: str,
    code_challenge: Optional[str] = None,
    extra_scopes: Optional[list[str]] = None,
) -> str:
    """Construye la URL completa donde redirigir al usuario para consentir.

    PKCE: si el provider lo soporta, exige code_challenge.
    """
    cfg = get_provider_cfg(provider)
    if not cfg:
        raise ValueError(f"Unknown provider: {provider}")

    client_id = os.getenv(cfg["client_id_env"])
    if not client_id:
        raise RuntimeError(
            f"Missing env var {cfg['client_id_env']} for provider {provider}"
        )

    # Auth URL · puede ser directo o con base+path (DocuSign)
    if "auth_url" in cfg:
        auth_url = _resolve_url(cfg["auth_url"], provider)
    else:
        base = os.getenv(cfg["auth_url_env"], "")
        if not base:
            raise RuntimeError(f"Missing env var {cfg['auth_url_env']}")
        auth_url = base.rstrip("/") + cfg["auth_path"]

    scopes = list(cfg["default_scopes"])
    if extra_scopes:
        for s in extra_scopes:
            if s not in scopes:
                scopes.append(s)

    params: dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": get_redirect_uri(),
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
    }
    # PKCE
    if cfg.get("uses_pkce") and code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    # Extras
    params.update(cfg.get("extra_auth_params", {}))

    return f"{auth_url}?{urlencode(params)}"


# ─────────────────────────────────────────────────────────────────────
# Code exchange
# ─────────────────────────────────────────────────────────────────────

async def exchange_code(
    provider: str,
    *,
    code: str,
    code_verifier: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Cambia el código de autorización por tokens.

    Retorna dict con keys: access_token, refresh_token (opcional),
    expires_in (segundos), scope, plus campos provider-específicos
    (account_id, email).
    """
    cfg = get_provider_cfg(provider)
    if not cfg:
        logger.error("exchange_code: unknown provider %s", provider)
        return None

    client_id = os.getenv(cfg["client_id_env"])
    client_secret = os.getenv(cfg["client_secret_env"])
    if not client_id or not client_secret:
        logger.error("exchange_code: missing credentials for %s", provider)
        return None

    # Token URL
    if "token_url" in cfg:
        token_url = _resolve_url(cfg["token_url"], provider)
    else:
        base = os.getenv(cfg["auth_url_env"], "")
        token_url = base.rstrip("/") + cfg["token_path"]

    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": get_redirect_uri(),
        "grant_type": "authorization_code",
    }
    if cfg.get("uses_pkce") and code_verifier:
        data["code_verifier"] = code_verifier

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                token_url,
                data=data,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            tokens = response.json()
    except Exception as e:
        logger.warning("exchange_code %s failed: %s", provider, e)
        return None

    # Normalizar respuesta
    result = {
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "expires_in": tokens.get("expires_in"),
        "scope": tokens.get("scope", ""),
        "token_type": tokens.get("token_type", "Bearer"),
    }
    # Enriquecer con userinfo (email, account_id) por provider
    if provider in ("google_drive", "google", "gmail"):
        userinfo = await _fetch_google_userinfo(result["access_token"])
        if userinfo:
            result["email"] = userinfo.get("email")
            result["account_id"] = userinfo.get("sub") or userinfo.get("id")
    elif provider in ("onedrive", "outlook"):
        userinfo = await _fetch_microsoft_userinfo(result["access_token"])
        if userinfo:
            result["email"] = userinfo.get("mail") or userinfo.get("userPrincipalName")
            result["account_id"] = userinfo.get("id")
    elif provider == "dropbox":
        userinfo = await _fetch_dropbox_userinfo(result["access_token"])
        if userinfo:
            result["email"] = userinfo.get("email")
            result["account_id"] = userinfo.get("account_id")
    elif provider == "docusign":
        userinfo = await _fetch_docusign_userinfo(result["access_token"])
        if userinfo:
            result["email"] = userinfo.get("email")
            result["account_id"] = userinfo.get("sub")

    return result


async def _fetch_google_userinfo(access_token: str) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.debug("google userinfo failed: %s", e)
        return None


async def _fetch_microsoft_userinfo(access_token: str) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://graph.microsoft.com/v1.0/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.debug("microsoft userinfo failed: %s", e)
        return None


async def _fetch_dropbox_userinfo(access_token: str) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                "https://api.dropboxapi.com/2/users/get_current_account",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            r.raise_for_status()
            data = r.json()
            return {
                "account_id": data.get("account_id"),
                "email": (data.get("email")
                          or data.get("name", {}).get("display_name")),
            }
    except Exception as e:
        logger.debug("dropbox userinfo failed: %s", e)
        return None


async def _fetch_docusign_userinfo(access_token: str) -> Optional[dict]:
    base = os.getenv("DOCUSIGN_ACCOUNT_BASE", "https://account-d.docusign.com")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{base.rstrip('/')}/oauth/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.debug("docusign userinfo failed: %s", e)
        return None


# ─────────────────────────────────────────────────────────────────────
# Refresh token
# ─────────────────────────────────────────────────────────────────────

async def refresh_access_token(
    provider: str,
    *,
    refresh_token: str,
) -> Optional[dict[str, Any]]:
    """Renueva el access_token usando refresh_token. Retorna mismo formato
    que exchange_code (sin userinfo · no es necesario en refresh)."""
    cfg = get_provider_cfg(provider)
    if not cfg:
        return None
    client_id = os.getenv(cfg["client_id_env"])
    client_secret = os.getenv(cfg["client_secret_env"])
    if not client_id or not client_secret:
        return None
    if "token_url" in cfg:
        token_url = _resolve_url(cfg["token_url"], provider)
    else:
        base = os.getenv(cfg["auth_url_env"], "")
        token_url = base.rstrip("/") + cfg["token_path"]
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                token_url, data=data, headers={"Accept": "application/json"}
            )
            r.raise_for_status()
            tokens = r.json()
        return {
            "access_token": tokens.get("access_token"),
            "refresh_token": tokens.get("refresh_token") or refresh_token,
            "expires_in": tokens.get("expires_in"),
            "scope": tokens.get("scope", ""),
        }
    except Exception as e:
        logger.warning("refresh %s failed: %s", provider, e)
        return None
