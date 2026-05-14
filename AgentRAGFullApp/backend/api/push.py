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
    from utils.vapid import is_configured, get_public_key
    if not is_configured():
        return {
            "configured": False,
            "instructions": (
                "Pide al admin que genere las keys desde Settings · Push (Sprint 12). "
                "Internamente POST /v1/push/admin/generate-vapid produce el par y "
                "te dice qué setear en Railway + Vercel."
            ),
        }
    return {"configured": True, "public_key": get_public_key()}


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
        sub_id = s["id"]
        http_status: Optional[int] = None
        err: Optional[str] = None
        ok = False
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
            ok = True
            http_status = 201
        except WebPushException as e:  # type: ignore
            failed += 1
            err = str(e)[:500]
            try:
                http_status = e.response.status_code if getattr(e, "response", None) is not None else None
            except Exception:
                http_status = None
            logger.warning("push failed sub=%s: %s", sub_id, e)
            try:
                if http_status == 410:
                    async with storage.pool.acquire() as conn:
                        await conn.execute(
                            "update push_subscriptions set active = false, last_status = 410, "
                            "last_error = 'gone' where id = $1::uuid",
                            sub_id,
                        )
            except Exception:
                pass
        except Exception as e:
            failed += 1
            err = str(e)[:500]
            logger.warning("push generic failed: %s", e)

        # Audit log (Sprint 12)
        try:
            async with storage.pool.acquire() as conn:
                await conn.execute(
                    """
                    insert into push_notifications_log
                      (firm_id, user_id, subscription_id, title, body, url, kind,
                       status, http_status, error)
                    values ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7,
                            $8, $9, $10)
                    """,
                    firm_id, user_id, sub_id, title, body, url,
                    "dispatch", "sent" if ok else "failed", http_status, err,
                )
        except Exception:
            pass
    return {"sent": sent, "failed": failed, "status": "ok"}


# ──────────────────────────────────────────────────────────────────────
# Sprint 12 · Admin endpoints (VAPID generation + dispatch test + logs)
# ──────────────────────────────────────────────────────────────────────


@router.post("/admin/generate-vapid")
async def admin_generate_vapid(principal: Principal = Depends(get_current_firm)):
    """Genera un par VAPID. El admin lo copia a env vars de Railway/Vercel.
    NO se persiste en DB — las keys viven en env vars del servidor."""
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin / socio_senior")
    from utils.vapid import generate_keys
    try:
        keys = generate_keys()
        return {
            "public_key": keys["public_key"],
            "private_key": keys["private_key"],
            "subject": keys["subject"],
            "instructions": (
                "Copia estas variables a Railway (backend) y Vercel (frontend):\n"
                f"VAPID_PUBLIC_KEY={keys['public_key']}\n"
                f"VAPID_PRIVATE_KEY={keys['private_key']}\n"
                f"VAPID_SUBJECT={keys['subject']}\n\n"
                "El frontend solo necesita VAPID_PUBLIC_KEY como NEXT_PUBLIC_VAPID_PUBLIC_KEY."
            ),
        }
    except Exception as e:
        raise HTTPException(500, f"generate failed: {e}")


@router.get("/admin/status")
async def admin_status(principal: Principal = Depends(get_current_firm)):
    """Devuelve si VAPID está configurado + counts del firm."""
    from utils.vapid import is_configured, get_public_key, get_subject
    from utils.db import get_storage
    storage = await get_storage()
    counts = {"subscriptions": 0, "subscriptions_active": 0, "sent_24h": 0, "failed_24h": 0}
    if hasattr(storage, "pool"):
        async with storage.pool.acquire() as conn:
            counts["subscriptions"] = await conn.fetchval(
                "select count(*) from push_subscriptions where firm_id = $1::uuid",
                principal.firm_id,
            )
            counts["subscriptions_active"] = await conn.fetchval(
                "select count(*) from push_subscriptions where firm_id = $1::uuid and active = true",
                principal.firm_id,
            )
            counts["sent_24h"] = await conn.fetchval(
                """select count(*) from push_notifications_log
                    where firm_id = $1::uuid and status = 'sent'
                      and sent_at >= now() - interval '24 hours'""",
                principal.firm_id,
            )
            counts["failed_24h"] = await conn.fetchval(
                """select count(*) from push_notifications_log
                    where firm_id = $1::uuid and status = 'failed'
                      and sent_at >= now() - interval '24 hours'""",
                principal.firm_id,
            )
    return {
        "vapid_configured": is_configured(),
        "vapid_public_key": get_public_key() if is_configured() else None,
        "vapid_subject": get_subject(),
        "counts": counts,
    }


@router.get("/admin/logs")
async def admin_logs(
    limit: int = 50,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior", "socio_junior"):
        raise HTTPException(403, "Solo socios/admin")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, user_id, subscription_id, title, body, url, kind,
                   status, http_status, error, sent_at
              from push_notifications_log
             where firm_id = $1::uuid
             order by sent_at desc
             limit $2
            """,
            principal.firm_id, min(limit, 200),
        )
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]),
                "user_id": str(r["user_id"]) if r["user_id"] else None,
                "subscription_id": str(r["subscription_id"]) if r["subscription_id"] else None,
                "title": r["title"], "body": r["body"], "url": r["url"], "kind": r["kind"],
                "status": r["status"], "http_status": r["http_status"], "error": r["error"],
                "sent_at": r["sent_at"].isoformat() if r["sent_at"] else None,
            }
            for r in rows
        ],
    }
