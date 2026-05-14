"""Sprint 12 · VAPID helper.

Genera y valida claves VAPID (Web Push). Sprint 5 dejó push como stub porque
nunca se generaron keys; ahora cualquier admin puede generarlas y persistirlas
en env vars desde la UI.

Variables de entorno requeridas en producción:
  VAPID_PUBLIC_KEY     · base64url, 65 bytes (P-256 uncompressed point)
  VAPID_PRIVATE_KEY    · base64url, 32 bytes (P-256 scalar)
  VAPID_SUBJECT        · 'mailto:admin@lexai.co' o https://...
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(os.getenv("VAPID_PUBLIC_KEY") and os.getenv("VAPID_PRIVATE_KEY"))


def get_public_key() -> Optional[str]:
    return os.getenv("VAPID_PUBLIC_KEY")


def get_subject() -> str:
    return os.getenv("VAPID_SUBJECT", "mailto:admin@lexai.co")


def generate_keys() -> dict:
    """Genera un par de claves VAPID válidas. Devuelve dict con public_key,
    private_key y subject sugerido.

    Implementación independiente de pywebpush (usa solo `cryptography`)
    para no requerir libs externas en el path de generación.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
    except ImportError as e:
        raise RuntimeError(
            "cryptography no instalada. Pip install cryptography>=42.0.0"
        ) from e

    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()

    # Private key: 32 bytes raw scalar (urlsafe base64 sin padding)
    priv_bytes = private_key.private_numbers().private_value.to_bytes(32, "big")
    private_b64 = _urlsafe_b64encode(priv_bytes)

    # Public key: 65 bytes uncompressed point (0x04 + X + Y)
    pub_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    public_b64 = _urlsafe_b64encode(pub_bytes)

    return {
        "public_key": public_b64,
        "private_key": private_b64,
        "subject": get_subject(),
    }


def _urlsafe_b64encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def validate_keys(public_key: str, private_key: str) -> tuple[bool, Optional[str]]:
    """Verifica que las keys decodifiquen a 65 y 32 bytes respectivamente."""
    try:
        pub = base64.urlsafe_b64decode(_pad(public_key))
        priv = base64.urlsafe_b64decode(_pad(private_key))
        if len(pub) != 65 or pub[0] != 0x04:
            return False, "public_key debe ser 65 bytes uncompressed (P-256)"
        if len(priv) != 32:
            return False, "private_key debe ser 32 bytes (P-256 scalar)"
        return True, None
    except Exception as e:
        return False, str(e)


def _pad(s: str) -> str:
    return s + "=" * (-len(s) % 4)
