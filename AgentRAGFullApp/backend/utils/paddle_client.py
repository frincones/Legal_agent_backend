"""Sprint 23 · Paddle client · wrappers HTTP + Mock para dev.

Diseño:
  - Una sola clase `PaddleClient` con métodos async.
  - Si `PADDLE_API_KEY` no está seteado o `LEXAI_PADDLE_FORCE_MOCK=1`,
    todos los métodos devuelven respuestas mock realistas
    (transaction_id 'mock_txn_...', checkout_url '/billing/checkout-success?demo=1').
  - Esto permite que el frontend funcione end-to-end en sandbox/dev
    sin depender de credenciales reales.

Métodos:
  - create_checkout(firm_id, plan_code, price_id, customer_email)
  - cancel_subscription(paddle_subscription_id)
  - get_subscription(paddle_subscription_id)
  - update_subscription_plan(paddle_subscription_id, new_price_id)
  - create_customer_portal_url(paddle_customer_id)
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


def _is_mock() -> bool:
    return (
        os.getenv("LEXAI_PADDLE_FORCE_MOCK", "").lower() in ("1", "true", "yes")
        or not os.getenv("PADDLE_API_KEY")
    )


def _base_url() -> str:
    return (
        "https://api.paddle.com"
        if os.getenv("PADDLE_ENV", "sandbox").lower() == "production"
        else "https://sandbox-api.paddle.com"
    )


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {os.getenv('PADDLE_API_KEY', '')}",
        "Content-Type": "application/json",
    }


class PaddleClient:
    """Async HTTP client para Paddle Billing API v1."""

    async def create_checkout(
        self,
        firm_id: str,
        plan_code: str,
        price_id: str,
        customer_email: Optional[str] = None,
    ) -> dict[str, Any]:
        if _is_mock():
            mock_id = f"mock_txn_{uuid.uuid4().hex[:12]}"
            return {
                "configured": False,
                "mock": True,
                "transaction_id": mock_id,
                "checkout_url": f"/billing/checkout-success?demo=1&plan={plan_code}&txn={mock_id}",
                "raw": {"id": mock_id, "status": "draft"},
            }
        payload = {
            "items": [{"price_id": price_id, "quantity": 1}],
            "custom_data": {"firm_id": str(firm_id), "plan_code": plan_code},
        }
        if customer_email:
            payload["customer_email"] = customer_email
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{_base_url()}/transactions", headers=_headers(), json=payload)
            data = r.json() or {}
            if r.status_code not in (200, 201):
                logger.warning("paddle checkout failed %s: %s", r.status_code, r.text[:200])
                return {"configured": True, "error": True, "status": r.status_code, "raw": data}
            d = data.get("data") or {}
            return {
                "configured": True,
                "mock": False,
                "transaction_id": d.get("id"),
                "checkout_url": (d.get("checkout") or {}).get("url"),
                "raw": d,
            }

    async def cancel_subscription(self, paddle_subscription_id: str) -> dict[str, Any]:
        if _is_mock():
            return {"mock": True, "ok": True, "status": "canceled"}
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{_base_url()}/subscriptions/{paddle_subscription_id}/cancel",
                headers=_headers(),
                json={"effective_from": "next_billing_period"},
            )
            ok = r.status_code in (200, 201, 202)
            return {"mock": False, "ok": ok, "status": r.status_code, "raw": r.json() if ok else r.text[:200]}

    async def get_subscription(self, paddle_subscription_id: str) -> dict[str, Any]:
        if _is_mock():
            return {"mock": True, "id": paddle_subscription_id, "status": "active"}
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{_base_url()}/subscriptions/{paddle_subscription_id}",
                headers=_headers(),
            )
            return {"mock": False, "status_code": r.status_code, "data": r.json() if r.status_code == 200 else None}

    async def update_subscription_plan(
        self,
        paddle_subscription_id: str,
        new_price_id: str,
    ) -> dict[str, Any]:
        if _is_mock():
            return {"mock": True, "ok": True, "new_price_id": new_price_id}
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.patch(
                f"{_base_url()}/subscriptions/{paddle_subscription_id}",
                headers=_headers(),
                json={
                    "items": [{"price_id": new_price_id, "quantity": 1}],
                    "proration_billing_mode": "prorated_immediately",
                },
            )
            return {
                "mock": False,
                "ok": r.status_code in (200, 201),
                "status": r.status_code,
                "raw": r.json() if r.status_code in (200, 201) else r.text[:200],
            }

    async def create_customer_portal_url(self, paddle_customer_id: str) -> dict[str, Any]:
        if _is_mock():
            return {
                "mock": True,
                "url": f"/settings/billing?portal=demo&customer={paddle_customer_id}",
            }
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{_base_url()}/customers/{paddle_customer_id}/portal-sessions",
                headers=_headers(),
                json={},
            )
            if r.status_code in (200, 201):
                d = (r.json() or {}).get("data") or {}
                return {
                    "mock": False,
                    "url": (d.get("urls") or {}).get("general", {}).get("overview"),
                    "raw": d,
                }
            return {"mock": False, "error": True, "status": r.status_code, "raw": r.text[:200]}


_singleton: Optional[PaddleClient] = None


def get_paddle_client() -> PaddleClient:
    global _singleton
    if _singleton is None:
        _singleton = PaddleClient()
    return _singleton


def is_paddle_configured() -> bool:
    return not _is_mock()
