"""Sprint 14 · API keys helpers.

Formato: lex_live_<random12>_<random32> donde:
  - 'lex_live_' es prefix conocido
  - random12 visible (prefix mostrado en UI)
  - random32 secreto (solo se ve UNA vez al crear)

Almacenamos en DB sha256(key_completa) en api_keys.key_hash. La key plain
nunca se persiste.
"""

from __future__ import annotations

import hashlib
import secrets


def generate_key() -> tuple[str, str, str]:
    """Devuelve (plain, prefix_visible, sha256_hash)."""
    random_visible = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
    random_secret = secrets.token_urlsafe(24)
    plain = f"lex_live_{random_visible}_{random_secret}"
    prefix = f"lex_live_{random_visible}"
    hashed = hashlib.sha256(plain.encode("utf-8")).hexdigest()
    return plain, prefix, hashed


def hash_key(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


VALID_SCOPES = {
    "read", "write", "admin",
    "matters.read", "matters.write",
    "clients.read", "clients.write",
    "leads.read", "leads.write",
    "documents.read", "documents.write",
    "insights.read",
    "billing.read",
    "search.read",
    "marketplace.read",
}


def validate_scopes(scopes: list[str]) -> tuple[bool, list[str]]:
    invalid = [s for s in scopes if s not in VALID_SCOPES]
    return len(invalid) == 0, invalid


def scope_allows(granted: list[str], required: str) -> bool:
    if "admin" in granted:
        return True
    if required in granted:
        return True
    # 'read' implica todas las *.read
    if "read" in granted and required.endswith(".read"):
        return True
    if "write" in granted and required.endswith(".write"):
        return True
    return False
