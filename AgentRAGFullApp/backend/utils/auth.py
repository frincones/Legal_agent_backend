"""Supabase JWT verification + multi-tenant principal extraction.

Used as a FastAPI dependency on every protected route. Validates the
Supabase access token using either:

  · Asymmetric (ES256/RS256) via JWKS — the modern Supabase default.
    Keys are fetched from {SUPABASE_URL}/auth/v1/.well-known/jwks.json
    and cached in-process for JWKS_CACHE_TTL seconds.

  · Symmetric (HS256) via SUPABASE_JWT_SECRET — legacy fallback used
    when no JWKS is available or when explicitly configured.

Custom claims (firm_id, role_lexai, cedula_profesional) are expected
to be injected at token issuance via a Supabase Auth Hook
(custom_access_token_hook). Routes that require multi-tenancy use
get_current_firm() which rejects tokens missing firm_id.
"""

from __future__ import annotations

import logging
import os
import time
import threading
import urllib.request
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException, status
from fastapi.security.utils import get_authorization_scheme_param

try:
    from jose import JWTError, jwt as jose_jwt
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "python-jose[cryptography] is required for JWT verification. "
        "Install with: pip install python-jose[cryptography]"
    ) from e

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET")  # legacy HS256 fallback
SUPABASE_JWT_AUDIENCE = os.getenv("SUPABASE_JWT_AUDIENCE", "authenticated")
SUPABASE_JWT_ISSUER = os.getenv("SUPABASE_JWT_ISSUER") or (
    f"{SUPABASE_URL}/auth/v1" if SUPABASE_URL else None
)
SUPABASE_JWKS_URL = os.getenv(
    "SUPABASE_JWKS_URL",
    f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json" if SUPABASE_URL else "",
)
JWKS_CACHE_TTL = int(os.getenv("SUPABASE_JWKS_CACHE_TTL", "3600"))  # 1 h

# JWKS in-process cache
_jwks_lock = threading.Lock()
_jwks_cache: dict = {"fetched_at": 0.0, "keys_by_kid": {}}


@dataclass(frozen=True)
class Principal:
    """The authenticated user as derived from the Supabase JWT."""
    user_id: str
    firm_id: Optional[str]
    role: str
    email: Optional[str]
    cedula_profesional: Optional[str]
    raw_claims: dict


def _fetch_jwks() -> dict:
    """Fetch JWKS from Supabase, return {kid: key_dict}. Cached for TTL."""
    if not SUPABASE_JWKS_URL:
        return {}
    now = time.time()
    with _jwks_lock:
        if (now - _jwks_cache["fetched_at"]) < JWKS_CACHE_TTL and _jwks_cache["keys_by_kid"]:
            return _jwks_cache["keys_by_kid"]
    try:
        req = urllib.request.Request(SUPABASE_JWKS_URL, headers={"User-Agent": "lexai-backend/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            import json as _json
            data = _json.loads(r.read().decode("utf-8"))
        keys = data.get("keys", []) if isinstance(data, dict) else []
        by_kid = {k.get("kid") or "default": k for k in keys}
        with _jwks_lock:
            _jwks_cache["fetched_at"] = now
            _jwks_cache["keys_by_kid"] = by_kid
        logger.info("Fetched %d JWKS key(s) from %s", len(by_kid), SUPABASE_JWKS_URL)
        return by_kid
    except Exception as e:
        logger.warning("JWKS fetch failed: %s", e)
        with _jwks_lock:
            return _jwks_cache["keys_by_kid"]


def _decode_jwt(token: str) -> dict:
    """Decode + validate a Supabase access token.

    Strategy:
      1. Inspect the unverified header. If kid + alg in (ES256/RS256) and JWKS
         is available, verify against the matching JWKS key.
      2. Otherwise fall back to HS256 + SUPABASE_JWT_SECRET.
      3. On any failure raise 401.
    """
    try:
        header = jose_jwt.get_unverified_header(token)
    except JWTError as e:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Malformed token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e

    alg = header.get("alg", "HS256")
    kid = header.get("kid")

    options = {"verify_aud": True, "verify_exp": True}
    decode_kwargs: dict = {
        "algorithms": [alg],
        "options": options,
        "audience": SUPABASE_JWT_AUDIENCE,
    }
    if SUPABASE_JWT_ISSUER:
        decode_kwargs["issuer"] = SUPABASE_JWT_ISSUER

    if alg in ("ES256", "RS256") and SUPABASE_JWKS_URL:
        keys = _fetch_jwks()
        if kid and kid in keys:
            decode_kwargs["key"] = keys[kid]
        elif keys:
            # Single-key projects often omit kid; pick the first key
            decode_kwargs["key"] = next(iter(keys.values()))
        else:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "JWKS unavailable; cannot verify token",
                headers={"WWW-Authenticate": "Bearer"},
            )
    else:
        if not SUPABASE_JWT_SECRET:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "Server misconfigured: SUPABASE_JWT_SECRET not set "
                "and JWKS not available",
            )
        decode_kwargs["key"] = SUPABASE_JWT_SECRET

    try:
        return jose_jwt.decode(token, **decode_kwargs)
    except JWTError as e:
        logger.debug("JWT decode failed: %s", e)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


def _principal_from_claims(claims: dict) -> Principal:
    """Build a Principal from raw JWT claims.

    Claims contract (custom hook adds firm_id/role/cedula_profesional):
      sub:                user_id (auth.users.id)
      email:              user email
      app_metadata.firm_id, app_metadata.role
      user_metadata.cedula_profesional
    Some hooks set these at top level; we accept both shapes.
    """
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "JWT missing 'sub'")

    app_md = claims.get("app_metadata") or {}
    user_md = claims.get("user_metadata") or {}

    firm_id = (
        claims.get("firm_id")
        or app_md.get("firm_id")
        or user_md.get("firm_id")
    )
    role = (
        claims.get("role_lexai")
        or app_md.get("role")
        or user_md.get("role")
        or "lawyer"
    )
    cedula = (
        claims.get("cedula_profesional")
        or app_md.get("cedula_profesional")
        or user_md.get("cedula_profesional")
    )

    return Principal(
        user_id=str(sub),
        firm_id=str(firm_id) if firm_id else None,
        role=str(role),
        email=claims.get("email"),
        cedula_profesional=str(cedula) if cedula else None,
        raw_claims=claims,
    )


def _extract_token(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, token = get_authorization_scheme_param(authorization)
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid Authorization scheme; expected Bearer",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


# ─────────────────────────────────────────────────────────────────────
# FastAPI dependencies
# ─────────────────────────────────────────────────────────────────────


def get_current_user(
    authorization: Optional[str] = Header(default=None),
) -> Principal:
    """Verify Supabase JWT and return Principal. Use as Depends() on routes."""
    token = _extract_token(authorization)
    claims = _decode_jwt(token)
    return _principal_from_claims(claims)


def get_current_firm(
    principal: Principal = Depends(get_current_user),
) -> Principal:
    """Like get_current_user but also requires a firm_id claim.

    Use on every multi-tenant route (matters, clients, agent runs, etc.).
    """
    if not principal.firm_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Account not associated with a firm. Complete onboarding first.",
        )
    return principal


def require_role(*allowed: str):
    """Factory: dependency that enforces principal.role ∈ allowed."""
    allowed_set = {r.lower() for r in allowed}

    def _dep(principal: Principal = Depends(get_current_firm)) -> Principal:
        if principal.role.lower() not in allowed_set:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Role '{principal.role}' not allowed. Required: {sorted(allowed_set)}",
            )
        return principal

    return _dep


# ─────────────────────────────────────────────────────────────────────
# Voice ticket (HMAC short-lived) — used by /v1/voice/ticket → WS auth
# ─────────────────────────────────────────────────────────────────────

import hmac
import hashlib
import json
import base64

VOICE_TICKET_SECRET = os.getenv("VOICE_TICKET_HMAC_SECRET", "")
VOICE_TICKET_TTL_SECONDS = int(os.getenv("VOICE_TICKET_TTL_SECONDS", "60"))


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def issue_voice_ticket(principal: Principal, matter_id: Optional[str] = None) -> dict:
    """Create a signed short-lived ticket the browser uses for the WS handshake."""
    if not VOICE_TICKET_SECRET:
        raise HTTPException(500, "Server misconfigured: VOICE_TICKET_HMAC_SECRET not set")
    now = int(time.time())
    payload = {
        "sub": principal.user_id,
        "firm_id": principal.firm_id,
        "role": principal.role,
        "matter_id": matter_id,
        "iat": now,
        "exp": now + VOICE_TICKET_TTL_SECONDS,
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    sig = hmac.new(VOICE_TICKET_SECRET.encode(), body, hashlib.sha256).digest()
    ticket = f"{_b64u(body)}.{_b64u(sig)}"
    return {
        "ticket": ticket,
        "expires_at": payload["exp"],
        "ttl_seconds": VOICE_TICKET_TTL_SECONDS,
    }


def verify_voice_ticket(ticket: str) -> dict:
    """Verify HMAC + expiry. Returns the payload or raises 401."""
    if not VOICE_TICKET_SECRET:
        raise HTTPException(500, "Server misconfigured: VOICE_TICKET_HMAC_SECRET not set")
    try:
        body_b64, sig_b64 = ticket.split(".", 1)
        body = _b64u_decode(body_b64)
        sig = _b64u_decode(sig_b64)
        expected = hmac.new(VOICE_TICKET_SECRET.encode(), body, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            raise ValueError("bad signature")
        payload = json.loads(body)
        if payload.get("exp", 0) < int(time.time()):
            raise ValueError("expired")
        return payload
    except Exception as e:
        logger.debug("voice ticket verification failed: %s", e)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or expired voice ticket",
        ) from e
