"""Sprint 6 · Symmetric encryption for secrets at rest.

Uses Fernet (AES-128 + HMAC-SHA256) for at-rest encryption of OAuth tokens
and IMAP passwords stored in `email_integrations`.

Key management:
  - LEXAI_ENCRYPTION_KEY env var (Fernet key, base64-urlsafe, 32 bytes)
  - Generate one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  - Rotate by adding LEXAI_ENCRYPTION_KEY_PREV and the decrypt() helper will
    try both before failing.

If no key is set, encrypt() raises (writes are gated server-side) and
decrypt() returns None gracefully (so historical NULL rows still work).

Versioning:
  We store encryption_version=1 alongside ciphertext so future migrations
  can detect what key/algorithm was used.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_ENV_PRIMARY = "LEXAI_ENCRYPTION_KEY"
_ENV_PREVIOUS = "LEXAI_ENCRYPTION_KEY_PREV"

CURRENT_VERSION = 1


def _get_fernets():
    try:
        from cryptography.fernet import Fernet, MultiFernet
    except ImportError:
        return None, None
    primary = os.getenv(_ENV_PRIMARY)
    previous = os.getenv(_ENV_PREVIOUS)
    keys = [k for k in (primary, previous) if k]
    if not keys:
        return None, None
    try:
        fernets = [Fernet(k.encode()) for k in keys]
    except Exception as e:
        logger.warning("invalid LEXAI_ENCRYPTION_KEY: %s", e)
        return None, None
    if len(fernets) == 1:
        return fernets[0], fernets[0]
    return MultiFernet(fernets), fernets[0]


def is_enabled() -> bool:
    """True if a primary encryption key is configured."""
    primary = os.getenv(_ENV_PRIMARY)
    return bool(primary)


def encrypt(plaintext: Optional[str]) -> Optional[bytes]:
    """Encrypt a string. Returns None for None input.

    Raises RuntimeError if no key is configured — callers should guard
    with is_enabled() or accept that writes will fail until the operator
    sets LEXAI_ENCRYPTION_KEY.
    """
    if plaintext is None:
        return None
    multi, primary = _get_fernets()
    if primary is None:
        raise RuntimeError(
            "LEXAI_ENCRYPTION_KEY no está configurada. "
            "Genera una con: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return primary.encrypt(plaintext.encode("utf-8"))


def decrypt(ciphertext: Optional[bytes]) -> Optional[str]:
    """Decrypt bytes back to a string. Returns None on missing input or
    if no key is configured (gracefully — doesn't crash the API)."""
    if ciphertext is None:
        return None
    multi, primary = _get_fernets()
    if multi is None:
        return None
    try:
        return multi.decrypt(bytes(ciphertext)).decode("utf-8")
    except Exception as e:
        logger.warning("decrypt failed: %s", e)
        return None


def encrypt_or_passthrough(plaintext: Optional[str]) -> tuple[Optional[bytes], int]:
    """Used for safe migration windows: if no key is set, returns (None, 0)
    so the caller can fall back to plaintext storage. Returns (bytes, 1)
    when encryption is active."""
    if plaintext is None:
        return None, 0
    if not is_enabled():
        return None, 0
    return encrypt(plaintext), CURRENT_VERSION
