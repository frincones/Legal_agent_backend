"""Sprint 5 · Web Push subscribe API.

Endpoints:
  GET  /v1/push/vapid-key       → public key (frontend la usa para subscribe)
  POST /v1/push/subscribe       → guarda PushSubscription
  DELETE /v1/push/subscriptions/{id}
  POST /v1/push/test            → envía notificación de prueba al usuario actual
                                  (solo si VAPID está configurado)

VAPID keys (env):
  VAPID_PUBLIC_KEY  · base64url, expuesta al frontend
  VAPID_PRIVATE_KEY · base64url, server-only
  VAPID_SUBJECT     · 'mailto:admin@lexai.co'

Si las claves no están seteadas, los endpoints siguen funcionando para guardar
suscripciones pero `/test` y el dispatch real harán no-op informativo.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/push", tags=["push"])


class SubscribeRequest(BaseModel):
    endpoint: str = Field(min_length=10)
    keys: dict
    user_agent: Optional[str] = None
    device_label: Optional[str] = None


@router.get("/vapid-key")
async def vapid_key():
    pub = os.getenv("VAPID_PUBLIC_KEY")
    if not pub:
        return {
            "configured": False,
            "instructions": (
                "Genera VAPID keys con: web-push generate-vapid-keys (npm i -g web-push)."
                " Luego setea VAPID_PUBLIC_KEY y VAPID_PRIVATE_KEY en Railway."
            ),
        }
    return {"configured": True, "public_key": pub}


@router.post("/subscribe")
async def subscribe(
    body: SubscribeRequest,
    principal: Principal = Depends(get_current_firm),
):
    p256dh = (body.keys or {}).get("p256dh")
    auth = (body.keys or {}).get("auth")
    if not (p256dh and auth):
        raise HTTPException(400, "keys.p256dh y keys.auth requeridos")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into push_subscriptions
              (firm_id, user_id, endpoint, p256dh, auth, user_agent, device_label)
            values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7)
            on conflict (user_id, endpoint) do update set
              p256dh = excluded.p256dh,
              auth = excluded.auth,
              user_agent = excluded.user_agent,
              device_label = excluded.device_label,
              active = true,
              updated_at = now()
            returning id
            """,
            principal.firm_id, principal.user_id,
            body.endpoint, p256dh, auth, body.user_agent, body.device_label,
        )
    return {"id": str(row["id"]), "ok": True}


@router.delete("/subscriptions/{subscription_id}")
async def delete_subscription(
    subscription_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "delete from push_subscriptions where id = $1::uuid and firm_id = $2::uuid",
            subscription_id, principal.firm_id,
        )
    return {"ok": True}


@router.post("/test")
async def push_test(principal: Principal = Depends(get_current_firm)):
    """Envía una notificación 'LexAI · prueba' al usuario actual."""
    return await dispatch_to_user(
        user_id=principal.user_id,
        firm_id=principal.firm_id,
        title="LexAI",
        body="Notificación de prueba · push activo en este dispositivo",
        url="/inbox",
    )


# ──────────────────────────────────────────────────────────────────────
# Dispatch · usado por workers (SLA, judicial poller post-hook, etc.)
# ──────────────────────────────────────────────────────────────────────


async def dispatch_to_user(
    user_id: str,
    firm_id: str,
    title: str,
    body: str,
    url: Optional[str] = None,
    icon: Optional[str] = None,
) -> dict:
    """Envía push a todas las suscripciones activas del usuario.

    Si VAPID no está configurado, marca el resultado como 'not_configured'
    pero no lanza error (idempotente para workers).
    """
    pub = os.getenv("VAPID_PUBLIC_KEY")
    priv = os.getenv("VAPID_PRIVATE_KEY")
    subj = os.getenv("VAPID_SUBJECT", "mailto:admin@lexai.co")
    if not (pub and priv):
        logger.info("push: VAPID no configurado, skip user=%s", user_id)
        return {"sent": 0, "status": "not_configured"}

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"sent": 0, "status": "no_storage"}
    async with storage.pool.acquire() as conn:
        subs = await conn.fetch(
            """
            select id, endpoint, p256dh, auth from push_subscriptions
             where user_id = $1::uuid and firm_id = $2::uuid and active = true
            """,
            user_id, firm_id,
        )
    if not subs:
        return {"sent": 0, "status": "no_subscriptions"}

    payload = json.dumps({"title": title, "body": body, "url": url, "icon": icon})
    sent, failed = 0, 0
    try:
        from pywebpush import webpush, WebPushException  # type: ignore
    except Exception:
        logger.info("pywebpush not installed; install with: pip install pywebpush")
        return {"sent": 0, "status": "library_missing"}

    for s in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": s["endpoint"],
                    "keys": {"p256dh": s["p256dh"], "auth": s["auth"]},
                },
                data=payload,
                vapid_private_key=priv,
                vapid_claims={"sub": subj},
                ttl=60 * 60 * 24,
            )
            sent += 1
        except WebPushException as e:  # type: ignore
            failed += 1
            logger.warning("push failed sub=%s: %s", s["id"], e)
            # 410 Gone → desactivar sub
            try:
                if getattr(e, "response", None) is not None and e.response.status_code == 410:
                    async with storage.pool.acquire() as conn:
                        await conn.execute(
                            "update push_subscriptions set active = false, last_status = 410, "
                            "last_error = 'gone' where id = $1::uuid",
                            s["id"],
                        )
            except Exception:
                pass
        except Exception as e:
            failed += 1
            logger.warning("push generic failed: %s", e)
    return {"sent": sent, "failed": failed, "status": "ok"}
