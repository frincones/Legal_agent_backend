"""Sprint 13 · Signature providers.

Adaptadores para Certicámara (CO) y DocuSign. Si no hay credenciales, queda
en modo 'demo' devolviendo URLs ficticias pero el flujo end-to-end funciona
para staging/demo.

Variables de entorno (opcionales):
  CERTICAMARA_API_KEY
  CERTICAMARA_API_BASE       (default sandbox)
  CERTICAMARA_ACCOUNT_ID

  DOCUSIGN_INTEGRATION_KEY
  DOCUSIGN_USER_ID
  DOCUSIGN_ACCOUNT_ID
  DOCUSIGN_BASE              (default https://demo.docusign.net)
  DOCUSIGN_PRIVATE_KEY       (RSA priv para JWT auth)

Sin estas vars, todos los provider quedan en provider='demo' y simulamos
el flujo completo (URLs locales, eventos manuales via admin endpoint).
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Optional

logger = logging.getLogger(__name__)


def get_available_providers() -> list[dict]:
    return [
        {
            "kind": "demo",
            "label": "Demo (sin firma legal)",
            "configured": True,
            "description": "Simula el flujo sin enviar realmente. Útil para staging.",
        },
        {
            "kind": "certicamara",
            "label": "Certicámara (Colombia)",
            "configured": bool(os.getenv("CERTICAMARA_API_KEY")),
            "description": "Firma electrónica con validez legal en Colombia (Ley 527/1999).",
        },
        {
            "kind": "docusign",
            "label": "DocuSign",
            "configured": bool(os.getenv("DOCUSIGN_INTEGRATION_KEY") and os.getenv("DOCUSIGN_PRIVATE_KEY")),
            "description": "DocuSign internacional. Compliance global.",
        },
    ]


async def create_envelope_remote(provider: str, envelope: dict, signers: list[dict], documents: list[dict]) -> dict:
    """Crea el envelope en el provider real. Devuelve external_id + signing_urls.

    envelope: {id, title, message, expires_at}
    signers: [{id, name, email, role, sort_order, auth_method}]
    documents: [{id, filename, storage_path_source, position}]
    """
    if provider == "certicamara" and os.getenv("CERTICAMARA_API_KEY"):
        return await _create_certicamara(envelope, signers, documents)
    if provider == "docusign" and os.getenv("DOCUSIGN_INTEGRATION_KEY"):
        return await _create_docusign(envelope, signers, documents)
    # Demo mode
    return _create_demo(envelope, signers, documents)


def _create_demo(envelope: dict, signers: list[dict], documents: list[dict]) -> dict:
    external_id = f"demo_{secrets.token_hex(8)}"
    signing_urls = {}
    for s in signers:
        token = secrets.token_urlsafe(24)
        signing_urls[str(s["id"])] = f"/portal/firma/{external_id}/{token}"
    return {
        "provider": "demo",
        "external_id": external_id,
        "signing_urls": signing_urls,
        "configured": False,
        "instructions": (
            "Modo demo activo: no se envió firma real. "
            "Configura CERTICAMARA_API_KEY o DOCUSIGN_INTEGRATION_KEY para producción."
        ),
    }


async def _create_certicamara(envelope: dict, signers: list[dict], documents: list[dict]) -> dict:
    """Stub real para Certicámara. La API exacta depende del producto contratado
    (FirmaYa Empresarial vs Solo Firma). Aquí implementamos una llamada genérica
    que el admin puede ajustar al endpoint específico de su contrato."""
    import httpx
    base = os.getenv("CERTICAMARA_API_BASE", "https://api.firmaya.gov.co/v1")
    key = os.getenv("CERTICAMARA_API_KEY")
    payload = {
        "title": envelope.get("title"),
        "message": envelope.get("message"),
        "signers": [
            {
                "name": s["name"], "email": s.get("email"),
                "identity": s.get("identity_id"),
                "auth_method": s.get("auth_method", "email"),
                "order": s.get("sort_order", 0),
            }
            for s in signers
        ],
        "documents": [
            {"filename": d["filename"], "position": d.get("position", 0)}
            for d in documents
        ],
        "expires_at": envelope.get("expires_at"),
    }
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{base}/envelopes", headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            }, json=payload)
            if r.status_code not in (200, 201):
                logger.warning("certicamara create failed: %s %s", r.status_code, r.text[:200])
                return _create_demo(envelope, signers, documents)
            data = r.json() or {}
            return {
                "provider": "certicamara",
                "external_id": data.get("envelope_id") or data.get("id"),
                "signing_urls": data.get("signing_urls") or {},
                "configured": True,
            }
    except Exception as e:
        logger.warning("certicamara error: %s", e)
        return _create_demo(envelope, signers, documents)


async def _create_docusign(envelope: dict, signers: list[dict], documents: list[dict]) -> dict:
    """Stub para DocuSign (eSignature REST API v2.1).
    Requiere JWT auth con private RSA key. Por simplicidad, este stub usa el
    integration key directamente (modo legacy); para producción real, el admin
    debe implementar el JWT consent flow."""
    import httpx
    base = os.getenv("DOCUSIGN_BASE", "https://demo.docusign.net")
    account_id = os.getenv("DOCUSIGN_ACCOUNT_ID")
    integration_key = os.getenv("DOCUSIGN_INTEGRATION_KEY")
    if not (base and account_id and integration_key):
        return _create_demo(envelope, signers, documents)

    payload = {
        "emailSubject": envelope.get("title"),
        "emailBlurb": envelope.get("message", ""),
        "status": "sent",
        "recipients": {
            "signers": [
                {
                    "email": s.get("email"),
                    "name": s["name"],
                    "recipientId": str(i + 1),
                    "routingOrder": str(s.get("sort_order", i + 1)),
                }
                for i, s in enumerate(signers)
            ],
        },
    }
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"{base}/restapi/v2.1/accounts/{account_id}/envelopes",
                headers={
                    "Authorization": f"Bearer {integration_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if r.status_code not in (200, 201):
                logger.warning("docusign create failed: %s %s", r.status_code, r.text[:200])
                return _create_demo(envelope, signers, documents)
            data = r.json() or {}
            return {
                "provider": "docusign",
                "external_id": data.get("envelopeId"),
                "signing_urls": {},
                "configured": True,
            }
    except Exception as e:
        logger.warning("docusign error: %s", e)
        return _create_demo(envelope, signers, documents)


def verify_webhook_signature(provider: str, secret: str, raw_body: bytes, signature: str) -> bool:
    """Valida signature de webhook entrante. Provider-específico."""
    if not (secret and signature):
        return False
    import hashlib
    import hmac
    try:
        if provider == "certicamara":
            # Certicámara: HMAC-SHA256(secret, raw_body) hex
            expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, signature)
        if provider == "docusign":
            # DocuSign Connect HMAC-SHA256 base64
            import base64
            expected = base64.b64encode(
                hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
            ).decode()
            return hmac.compare_digest(expected, signature)
    except Exception:
        return False
    return True  # demo accepts


def parse_webhook_event(provider: str, payload: dict) -> dict:
    """Normaliza el evento del provider a una forma común."""
    if provider == "certicamara":
        return {
            "external_envelope_id": payload.get("envelope_id"),
            "kind": payload.get("event"),                    # 'signed','viewed','declined','expired'
            "external_signer_id": payload.get("signer_id"),
            "signer_email": payload.get("signer_email"),
            "occurred_at": payload.get("timestamp"),
            "raw": payload,
        }
    if provider == "docusign":
        return {
            "external_envelope_id": payload.get("data", {}).get("envelopeId"),
            "kind": _docusign_event_to_kind(payload.get("event")),
            "external_signer_id": payload.get("data", {}).get("recipientId"),
            "signer_email": (payload.get("data", {}).get("recipientEmail")),
            "occurred_at": payload.get("eventDateTime"),
            "raw": payload,
        }
    return {"kind": "unknown", "raw": payload}


def _docusign_event_to_kind(event: Optional[str]) -> str:
    if not event:
        return "unknown"
    m = {
        "recipient-completed": "signed",
        "recipient-viewed": "viewed",
        "recipient-declined": "declined",
        "envelope-completed": "envelope_signed",
        "envelope-expired": "expired",
        "envelope-voided": "canceled",
    }
    return m.get(event, event)
