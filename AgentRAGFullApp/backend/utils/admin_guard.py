"""Sprint 24 · SaaS Admin guard + auto-audit dependency.

Diseño:
  - `require_saas_admin` es una FastAPI dependency que:
       1. Verifica JWT válido (vía get_current_user, NO get_current_firm — admins
          pueden no tener firm).
       2. Comprueba que el auth.uid del JWT esté en admin_users.active=true.
       3. Devuelve el `AdminPrincipal` con admin_user_id, role, email.
  - `require_saas_admin_role(*roles)` añade restricción por rol admin.
  - `audit_admin_action(...)` registra en audit_logs con metadata.scope='saas_admin'.

Roles admin: 'owner' | 'admin' | 'support' | 'readonly'
  · owner    → todo (incluyendo gestionar admin_users)
  · admin    → todo excepto admin_users (que está reservado a owners)
  · support  → tickets + ver firms + ver users (no modify)
  · readonly → solo lectura

Configuración:
  · Set bootstrap admin: ejecutar INSERT directo en SQL o set LEXAI_BOOTSTRAP_ADMIN_EMAIL
    env var → on first request, si el email del JWT == env var, auto-inserta como 'owner'.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, Request, status

from utils.auth import Principal, get_current_user

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdminPrincipal:
    admin_user_id: str
    auth_user_id: str
    email: str
    role: str
    full_name: Optional[str]
    raw_principal: Principal


async def _ensure_bootstrap_admin(principal: Principal) -> None:
    """Si LEXAI_BOOTSTRAP_ADMIN_EMAIL está seteado y matches el JWT,
    auto-crea como 'owner' si no existe."""
    bootstrap_email = os.getenv("LEXAI_BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
    if not bootstrap_email or not principal.email:
        return
    if principal.email.strip().lower() != bootstrap_email:
        return
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            insert into admin_users (auth_user_id, email, full_name, role, active)
            values ($1::uuid, $2, $3, 'owner', true)
            on conflict (email) do update set
              auth_user_id = excluded.auth_user_id,
              active = true
            """,
            principal.user_id,
            principal.email.lower(),
            (principal.raw_claims or {}).get("name") or principal.email,
        )
    logger.info("admin bootstrap: %s registered as owner", bootstrap_email)


async def require_saas_admin(
    principal: Principal = Depends(get_current_user),
) -> AdminPrincipal:
    """FastAPI dependency · gate de todos los endpoints /v1/admin/*."""
    if not principal.user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No autenticado")

    # Bootstrap path (no-op si LEXAI_BOOTSTRAP_ADMIN_EMAIL no está seteado)
    await _ensure_bootstrap_admin(principal)

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, auth_user_id, email, full_name, role, active
              from admin_users
             where auth_user_id = $1::uuid
            """,
            principal.user_id,
        )
    if not row or not row["active"]:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Acceso restringido al panel SaaS admin",
        )
    # Best-effort: actualizar last_login_at (sin bloquear)
    try:
        async with storage.pool.acquire() as conn:
            await conn.execute(
                "update admin_users set last_login_at = now() where id = $1",
                row["id"],
            )
    except Exception:
        pass
    return AdminPrincipal(
        admin_user_id=str(row["id"]),
        auth_user_id=str(row["auth_user_id"]),
        email=row["email"],
        role=row["role"],
        full_name=row["full_name"],
        raw_principal=principal,
    )


def require_saas_admin_role(*allowed: str):
    """Factory · dependency que además exige role admin ∈ allowed."""
    allowed_set = {r.lower() for r in allowed}

    async def _dep(admin: AdminPrincipal = Depends(require_saas_admin)) -> AdminPrincipal:
        if admin.role.lower() not in allowed_set:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Rol {admin.role} no autorizado · requiere {sorted(allowed_set)}",
            )
        return admin

    return _dep


async def audit_admin_action(
    admin: AdminPrincipal,
    action: str,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    firm_id: Optional[str] = None,
    outcome: str = "success",
    reason: Optional[str] = None,
    metadata: Optional[dict] = None,
    request: Optional[Request] = None,
) -> None:
    """Registra acción admin en audit_logs con scope='saas_admin'.

    No bloquea · si falla, solo loggea y sigue.
    """
    import json as _json
    payload = dict(metadata or {})
    payload["scope"] = "saas_admin"
    payload["admin_email"] = admin.email
    payload["admin_role"] = admin.role
    payload["admin_user_id"] = admin.admin_user_id

    ip = None
    user_agent = None
    if request is not None:
        ip = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")

    from utils.db import get_storage
    try:
        storage = await get_storage()
        async with storage.pool.acquire() as conn:
            await conn.execute(
                """
                insert into audit_logs
                  (firm_id, user_id, action, resource_type, resource_id,
                   ip_address, user_agent, outcome, reason, metadata)
                values
                  ($1::uuid, $2::uuid, $3, $4, $5, $6::inet, $7, $8, $9, $10::jsonb)
                """,
                firm_id,
                admin.auth_user_id,
                action,
                resource_type,
                resource_id,
                ip,
                user_agent,
                outcome,
                reason,
                _json.dumps(payload),
            )
    except Exception as e:
        logger.warning("audit_admin_action failed: %s", e)
