"""Sprint 6 · Billing API (Paddle).

Endpoints REST:
  GET  /v1/billing/plans                   · catálogo público de planes
  GET  /v1/billing/subscription            · suscripción de la firma actual
  POST /v1/billing/checkout                · crea sesión de checkout (devuelve URL)
  POST /v1/billing/portal                  · URL del customer portal Paddle
  POST /v1/billing/cancel                  · cancela al final del periodo (idempotente)
  POST /v1/billing/paddle/webhook          · webhook entrante Paddle (signed)

Variables de entorno requeridas en producción:
  PADDLE_API_KEY            · server-only (Paddle Customer/Subscription API)
  PADDLE_PUBLIC_TOKEN       · cliente (devuelta a frontend para Paddle.js)
  PADDLE_WEBHOOK_SECRET     · valida HMAC del webhook
  PADDLE_VENDOR_ID          · opcional
  PADDLE_ENV                · 'sandbox' | 'production' (default sandbox)

Si las variables NO están seteadas:
  - checkout/portal devuelven 503 con un mensaje útil
  - webhooks responden 200 OK (no rompen Paddle retry)
  - GET /plans y /subscription siguen funcionando normalmente
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/billing", tags=["billing"])


def _paddle_base() -> str:
    return (
        "https://api.paddle.com"
        if os.getenv("PADDLE_ENV", "sandbox").lower() == "production"
        else "https://sandbox-api.paddle.com"
    )


def _paddle_configured() -> bool:
    return bool(os.getenv("PADDLE_API_KEY"))


# ──────────────────────────────────────────────────────────────────────
# Plans · catálogo
# ──────────────────────────────────────────────────────────────────────


@router.get("/plans")
async def list_plans():
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select code, name, monthly_cop, annual_cop, paddle_price_id,
                   paddle_price_id_annual,
                   q_users, q_matters, q_documents_mo, q_llm_calls_mo,
                   q_voice_min_mo, q_email_accounts, q_judicial_subs,
                   f_court_watcher, f_email_ingest, f_voice, f_canvas,
                   f_calc, f_briefing, f_priority_support
              from subscription_plans
             order by monthly_cop asc
            """,
        )
    return {
        "paddle_configured": _paddle_configured(),
        "paddle_public_token": os.getenv("PADDLE_PUBLIC_TOKEN") if _paddle_configured() else None,
        "paddle_env": os.getenv("PADDLE_ENV", "sandbox"),
        "items": [dict(r) for r in rows],
    }


# ──────────────────────────────────────────────────────────────────────
# Suscripción actual + usage
# ──────────────────────────────────────────────────────────────────────


@router.get("/subscription")
async def my_subscription(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        result = await conn.fetchval("select lexai_firm_usage($1::uuid)", principal.firm_id)
    return result if result is not None else {"plan": {"code": "free"}, "usage": {}}


# ──────────────────────────────────────────────────────────────────────
# Checkout · crea Paddle Transaction y devuelve URL
# ──────────────────────────────────────────────────────────────────────


class CheckoutRequest(BaseModel):
    plan_code: str = Field(pattern="^(pro|firm|enterprise)$")
    billing_period: str = Field(default="monthly", pattern="^(monthly|annual)$")


@router.post("/checkout")
async def create_checkout(
    body: CheckoutRequest,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin / socio_senior puede contratar planes")
    if not _paddle_configured():
        return {
            "configured": False,
            "message": (
                "Paddle no configurado. Setea PADDLE_API_KEY, PADDLE_PUBLIC_TOKEN, "
                "PADDLE_WEBHOOK_SECRET en Railway. Mientras tanto, contáctanos para "
                "activar tu plan manualmente."
            ),
        }
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        plan = await conn.fetchrow(
            "select paddle_price_id, paddle_price_id_annual, name from subscription_plans where code = $1",
            body.plan_code,
        )
    if not plan:
        raise HTTPException(404, "plan no existe")
    price_id = plan["paddle_price_id_annual"] if body.billing_period == "annual" else plan["paddle_price_id"]
    if not price_id:
        return {"configured": False, "message": f"Plan {body.plan_code} no tiene paddle_price_id configurado"}

    import httpx
    payload = {
        "items": [{"price_id": price_id, "quantity": 1}],
        "custom_data": {"firm_id": str(principal.firm_id), "plan_code": body.plan_code},
        "customer_email": principal.email if hasattr(principal, "email") else None,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{_paddle_base()}/transactions",
                headers={
                    "Authorization": f"Bearer {os.getenv('PADDLE_API_KEY')}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if r.status_code not in (200, 201):
                logger.warning("paddle checkout failed: %s %s", r.status_code, r.text[:200])
                raise HTTPException(502, f"paddle error: {r.status_code}")
            data = r.json() or {}
            return {
                "configured": True,
                "transaction_id": (data.get("data") or {}).get("id"),
                "checkout_url": (data.get("data") or {}).get("checkout", {}).get("url"),
                "raw": data.get("data"),
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("checkout error")
        raise HTTPException(502, f"checkout error: {e}")


@router.post("/cancel")
async def cancel_subscription(principal: Principal = Depends(get_current_firm)):
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin puede cancelar")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        sub = await conn.fetchrow(
            "select paddle_subscription_id from firm_subscriptions where firm_id = $1::uuid",
            principal.firm_id,
        )
    if not sub or not sub["paddle_subscription_id"]:
        # Sin Paddle activo: marcamos canceled localmente
        async with storage.pool.acquire() as conn:
            await conn.execute(
                """
                insert into firm_subscriptions (firm_id, plan_code, status, canceled_at)
                values ($1::uuid, 'free', 'canceled', now())
                on conflict (firm_id) do update set status = 'canceled', canceled_at = now()
                """,
                principal.firm_id,
            )
        return {"ok": True, "via": "local"}
    if not _paddle_configured():
        return {"configured": False, "message": "Paddle no configurado"}
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{_paddle_base()}/subscriptions/{sub['paddle_subscription_id']}/cancel",
                headers={"Authorization": f"Bearer {os.getenv('PADDLE_API_KEY')}"},
                json={"effective_from": "next_billing_period"},
            )
            if r.status_code not in (200, 201, 202):
                raise HTTPException(502, f"paddle cancel HTTP {r.status_code}")
    except Exception as e:
        raise HTTPException(502, f"cancel error: {e}")
    return {"ok": True, "via": "paddle"}


# ──────────────────────────────────────────────────────────────────────
# Webhook · ingreso de eventos Paddle
# ──────────────────────────────────────────────────────────────────────


def _verify_paddle_signature(secret: str, raw_body: bytes, sig_header: str) -> bool:
    """Paddle envía 'ts=...;h1=...' en Paddle-Signature header.
    HMAC = HMAC-SHA256(secret, f'{ts}:{raw_body}')."""
    if not (secret and sig_header):
        return False
    try:
        parts = dict(p.split("=", 1) for p in sig_header.split(";") if "=" in p)
        ts = parts.get("ts")
        h1 = parts.get("h1")
        if not (ts and h1):
            return False
        signed = f"{ts}:{raw_body.decode('utf-8')}".encode("utf-8")
        expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, h1)
    except Exception as e:
        logger.warning("paddle sig verify failed: %s", e)
        return False


@router.post("/paddle/webhook")
async def paddle_webhook(
    request: Request,
    paddle_signature: Optional[str] = Header(default=None, alias="Paddle-Signature"),
):
    raw = await request.body()
    secret = os.getenv("PADDLE_WEBHOOK_SECRET")
    if secret and not _verify_paddle_signature(secret, raw, paddle_signature or ""):
        logger.warning("paddle webhook rejected: bad signature")
        # Devolvemos 200 igual para que Paddle no retry-spam, pero loggeamos.
        return {"ok": False, "reason": "bad_signature"}
    try:
        event = json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        return {"ok": False, "reason": "bad_json"}

    event_id = event.get("event_id") or event.get("id") or ""
    event_type = event.get("event_type") or event.get("type") or ""
    data = event.get("data") or {}

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"ok": False, "reason": "no_storage"}

    # Dedup por event_id
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            insert into paddle_webhook_events
              (event_id, event_type, paddle_customer_id, paddle_subscription_id, payload)
            values ($1, $2, $3, $4, $5::jsonb)
            on conflict (event_id) do nothing
            """,
            event_id or f"unknown-{datetime.now(timezone.utc).isoformat()}",
            event_type,
            (data.get("customer_id") or data.get("customer", {}).get("id")),
            (data.get("subscription_id") or data.get("id") if event_type.startswith("subscription.") else None),
            json.dumps(event),
        )

    # Procesamiento mínimo: subscription.created / updated / canceled → upsert firm_subscriptions
    try:
        firm_id = (data.get("custom_data") or {}).get("firm_id")
        plan_code = (data.get("custom_data") or {}).get("plan_code") or "pro"
        if not firm_id and event_type.startswith("subscription."):
            # Sin custom_data no podemos linkear; lo dejamos en webhook_events sin procesar
            return {"ok": True, "linked": False, "event_id": event_id}
        if event_type in ("subscription.created", "subscription.updated", "subscription.activated"):
            status_map = {"active": "active", "trialing": "trialing", "past_due": "past_due", "paused": "paused"}
            status = status_map.get(data.get("status", "active"), "active")
            sub_id = data.get("id")
            customer_id = data.get("customer_id")
            current_period_start = (data.get("current_billing_period") or {}).get("starts_at")
            current_period_end = (data.get("current_billing_period") or {}).get("ends_at")
            # Sprint F · respetar override manual del admin SaaS
            # Si overrides.manual_plan_override=true, NO sobrescribir plan_code
            # (admin lo cambió manualmente; webhook solo actualiza periodos/customer/status)
            async with storage.pool.acquire() as conn:
                existing = await conn.fetchrow(
                    "select overrides from firm_subscriptions where firm_id = $1::uuid",
                    firm_id,
                )
            has_manual_override = False
            if existing and existing.get("overrides"):
                ov = existing["overrides"]
                if isinstance(ov, str):
                    try:
                        ov = json.loads(ov)
                    except Exception:
                        ov = {}
                has_manual_override = bool((ov or {}).get("manual_plan_override"))

            if has_manual_override:
                # Webhook solo actualiza datos NO-críticos (periodos, customer_id, status)
                # plan_code preservado por respeto al override admin
                logger.info(
                    "Paddle webhook skipped plan_code overwrite for firm %s · manual_plan_override=true",
                    firm_id,
                )
                async with storage.pool.acquire() as conn:
                    await conn.execute(
                        """
                        update firm_subscriptions set
                          status = $2,
                          paddle_customer_id = $3,
                          paddle_subscription_id = $4,
                          current_period_start = $5::timestamptz,
                          current_period_end = $6::timestamptz,
                          updated_at = now()
                         where firm_id = $1::uuid
                        """,
                        firm_id, status, customer_id, sub_id,
                        current_period_start, current_period_end,
                    )
            else:
                async with storage.pool.acquire() as conn:
                    await conn.execute(
                        """
                        insert into firm_subscriptions
                          (firm_id, plan_code, status, paddle_customer_id, paddle_subscription_id,
                           current_period_start, current_period_end)
                        values ($1::uuid, $2, $3, $4, $5, $6::timestamptz, $7::timestamptz)
                        on conflict (firm_id) do update set
                          plan_code = excluded.plan_code,
                          status = excluded.status,
                          paddle_customer_id = excluded.paddle_customer_id,
                          paddle_subscription_id = excluded.paddle_subscription_id,
                          current_period_start = excluded.current_period_start,
                          current_period_end = excluded.current_period_end,
                          updated_at = now()
                        """,
                        firm_id, plan_code, status, customer_id, sub_id,
                        current_period_start, current_period_end,
                    )
        elif event_type in ("subscription.canceled", "subscription.paused"):
            async with storage.pool.acquire() as conn:
                await conn.execute(
                    """
                    update firm_subscriptions set
                      status = 'canceled', canceled_at = now(), updated_at = now()
                     where firm_id = $1::uuid
                    """,
                    firm_id,
                )
        # Marcar processed
        async with storage.pool.acquire() as conn:
            await conn.execute(
                "update paddle_webhook_events set processed = true, processed_at = now() where event_id = $1",
                event_id,
            )
    except Exception as e:
        logger.exception("paddle webhook handler error")
        async with storage.pool.acquire() as conn:
            await conn.execute(
                "update paddle_webhook_events set error = $2 where event_id = $1",
                event_id, str(e)[:500],
            )
        return {"ok": False, "error": str(e)[:200]}

    return {"ok": True, "event_type": event_type}


# ══════════════════════════════════════════════════════════════════════
# Sprint 23 · Quota status + start trial + portal (mock + Paddle)
# ══════════════════════════════════════════════════════════════════════


@router.get("/current-usage")
async def current_usage(principal: Principal = Depends(get_current_firm)):
    """Snapshot completo plan + cuotas + uso + flags (over_quota, near_80, near_95).

    Pensado para QuotaBanner + dashboards. Más rico que /subscription
    (que se mantiene por compat).
    """
    from utils.quota_tracker import status
    return await status(principal.firm_id)


@router.get("/quota-check")
async def quota_check(
    kind: str,
    amount: int = 1,
    principal: Principal = Depends(get_current_firm),
):
    """Precheck ligero para una operación específica.

    kind: 'llm_call' | 'voice_minute' | 'document_upload' | 'email_sync' | 'judicial_poll'
    """
    from utils.quota_tracker import precheck
    return await precheck(principal.firm_id, kind, amount)


class StartTrialRequest(BaseModel):
    plan_code: str = Field(default="free", pattern="^(free|pro|firm)$")


@router.post("/start-trial")
async def start_trial(
    body: StartTrialRequest,
    principal: Principal = Depends(get_current_firm),
):
    """Inicia trial de 14 días para el plan indicado (default free).

    Si la firma ya tiene un plan distinto a 'free' o status no es 'trialing',
    no hace nada (idempotente).
    """
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        existing = await conn.fetchrow(
            "select plan_code, status, trial_ends_at from firm_subscriptions where firm_id = $1::uuid",
            principal.firm_id,
        )
        if existing and existing["plan_code"] not in ("free",):
            return {"ok": True, "already_subscribed": True, "plan": existing["plan_code"]}
        await conn.execute(
            """
            insert into firm_subscriptions
              (firm_id, plan_code, status, trial_ends_at,
               current_period_start, current_period_end)
            values
              ($1::uuid, $2, 'trialing', now() + interval '14 days',
               date_trunc('month', now()),
               date_trunc('month', now()) + interval '1 month')
            on conflict (firm_id) do update set
              plan_code = excluded.plan_code,
              status = 'trialing',
              trial_ends_at = excluded.trial_ends_at,
              updated_at = now()
            """,
            principal.firm_id, body.plan_code,
        )
    return {"ok": True, "plan_code": body.plan_code, "status": "trialing"}


@router.post("/portal")
async def customer_portal(principal: Principal = Depends(get_current_firm)):
    """Devuelve URL del Paddle Customer Portal (o mock si no configurado)."""
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin / socio_senior puede acceder al portal")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        sub = await conn.fetchrow(
            "select paddle_customer_id from firm_subscriptions where firm_id = $1::uuid",
            principal.firm_id,
        )
    customer_id = sub["paddle_customer_id"] if sub else None
    if not customer_id:
        return {
            "configured": False,
            "url": None,
            "message": "Aún no tienes un plan Pro/Firm activo. Comienza con un upgrade primero.",
        }
    from utils.paddle_client import get_paddle_client
    client = get_paddle_client()
    result = await client.create_customer_portal_url(customer_id)
    return result


@router.post("/checkout/v2")
async def checkout_v2(
    body: CheckoutRequest,
    principal: Principal = Depends(get_current_firm),
):
    """Checkout v2 · usa PaddleClient con fallback a mock.

    Devuelve siempre checkout_url (mock o real) para que el frontend
    siempre tenga un URL al que redirigir.
    """
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin / socio_senior puede contratar planes")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        plan = await conn.fetchrow(
            "select paddle_price_id, paddle_price_id_annual, name from subscription_plans where code = $1",
            body.plan_code,
        )
    if not plan:
        raise HTTPException(404, "plan no existe")
    price_id = plan["paddle_price_id_annual"] if body.billing_period == "annual" else plan["paddle_price_id"]
    # En mock, price_id puede ser null y aún funciona
    from utils.paddle_client import get_paddle_client
    client = get_paddle_client()
    result = await client.create_checkout(
        firm_id=principal.firm_id,
        plan_code=body.plan_code,
        price_id=price_id or f"mock_price_{body.plan_code}",
        customer_email=principal.email,
    )
    return result


# ══════════════════════════════════════════════════════════════════════
# Mock checkout success (para demo sin Paddle)
# ══════════════════════════════════════════════════════════════════════


@router.post("/mock-activate")
async def mock_activate(
    body: CheckoutRequest,
    principal: Principal = Depends(get_current_firm),
):
    """Activa el plan localmente sin pasar por Paddle.
    Solo disponible cuando Paddle NO está configurado (modo demo)."""
    if _paddle_configured():
        raise HTTPException(400, "Paddle está configurado · usa /checkout en su lugar")
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin / socio_senior puede activar planes")
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            insert into firm_subscriptions
              (firm_id, plan_code, status, billing_period,
               current_period_start, current_period_end,
               paddle_customer_id, paddle_subscription_id)
            values
              ($1::uuid, $2, 'active', $3,
               date_trunc('month', now()),
               date_trunc('month', now()) + interval '1 month',
               $4, $5)
            on conflict (firm_id) do update set
              plan_code = excluded.plan_code,
              status = 'active',
              billing_period = excluded.billing_period,
              current_period_start = excluded.current_period_start,
              current_period_end = excluded.current_period_end,
              paddle_customer_id = excluded.paddle_customer_id,
              paddle_subscription_id = excluded.paddle_subscription_id,
              updated_at = now()
            """,
            principal.firm_id, body.plan_code, body.billing_period,
            f"mock_cust_{principal.firm_id}", f"mock_sub_{principal.firm_id}",
        )
    return {"ok": True, "mock": True, "plan": body.plan_code, "billing_period": body.billing_period}
