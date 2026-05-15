"""Sprint D · Cliente DocuSign · REST API directo (no MCP por estabilidad).

Implementa el subset que LexAI necesita:
  - create_envelope() · crea envelope + envía
  - get_envelope() · estado actual
  - resend() · re-notificar firmantes
  - void() · anular envelope
  - download_signed_pdf() · descarga PDF firmado para archivar

Demo base: https://demo.docusign.net/restapi
Prod base: https://www.docusign.net/restapi

Auth: Bearer access_token (OAuth2 Authorization Code Grant del usuario).
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


def _api_base() -> str:
    """Base URL del REST API según ambiente."""
    account_base = os.getenv("DOCUSIGN_ACCOUNT_BASE", "https://account-d.docusign.com")
    # Demo: api base es demo.docusign.net · Prod: www.docusign.net
    if "account-d" in account_base:
        return "https://demo.docusign.net/restapi"
    return "https://www.docusign.net/restapi"


class DocuSignClient:
    def __init__(self, access_token: str, account_id: str):
        self.access_token = access_token
        self.account_id = account_id

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def get_user_account_id(self) -> Optional[str]:
        """Obtiene el accountId del usuario · necesario para construir paths."""
        account_base = os.getenv("DOCUSIGN_ACCOUNT_BASE", "https://account-d.docusign.com")
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                f"{account_base.rstrip('/')}/oauth/userinfo",
                headers={"Authorization": f"Bearer {self.access_token}"},
            )
            if r.status_code != 200:
                return None
            data = r.json()
            accounts = data.get("accounts", [])
            for a in accounts:
                if a.get("is_default"):
                    return a.get("account_id")
            return accounts[0].get("account_id") if accounts else None

    async def create_envelope(
        self,
        *,
        title: str,
        message: str,
        document_name: str,
        document_bytes: bytes,
        signers: list[dict],  # [{name, email, recipient_id, routing_order}, ...]
        email_subject: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Crea envelope con un documento + signers · lo envía inmediatamente."""
        base = _api_base()
        url = f"{base}/v2.1/accounts/{self.account_id}/envelopes"
        doc_b64 = base64.b64encode(document_bytes).decode("ascii")
        # Detectar extensión
        ext = "pdf"
        if document_name.lower().endswith(".docx"):
            ext = "docx"
        elif document_name.lower().endswith(".doc"):
            ext = "doc"

        recipients_signers = []
        for idx, s in enumerate(signers, start=1):
            recipient_id = str(s.get("recipient_id", idx))
            routing_order = str(s.get("routing_order", idx))
            sig_tab = {
                "anchorString": "/sn" + str(idx) + "/",
                "anchorUnits": "pixels",
                "anchorXOffset": "0",
                "anchorYOffset": "0",
                # Si el doc no tiene anchors, DocuSign los pone al final
            }
            recipients_signers.append({
                "email": s["email"],
                "name": s.get("name") or s["email"],
                "recipientId": recipient_id,
                "routingOrder": routing_order,
                "tabs": {"signHereTabs": [sig_tab]},
            })

        payload = {
            "emailSubject": email_subject or title,
            "emailBlurb": message,
            "documents": [{
                "documentBase64": doc_b64,
                "documentId": "1",
                "fileExtension": ext,
                "name": document_name,
            }],
            "recipients": {"signers": recipients_signers},
            "status": "sent",  # 'sent' = enviar inmediatamente · 'created' = draft
        }

        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(url, headers=self._headers(), json=payload)
            if r.status_code not in (200, 201):
                logger.warning("docusign create_envelope failed: %s %s", r.status_code, r.text[:200])
                return None
            return r.json()

    async def get_envelope(self, envelope_id: str) -> Optional[dict]:
        base = _api_base()
        url = f"{base}/v2.1/accounts/{self.account_id}/envelopes/{envelope_id}"
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(url, headers=self._headers())
            if r.status_code != 200:
                return None
            return r.json()

    async def resend(self, envelope_id: str) -> bool:
        base = _api_base()
        url = f"{base}/v2.1/accounts/{self.account_id}/envelopes/{envelope_id}"
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.put(
                url,
                headers=self._headers(),
                params={"resend_envelope": "true"},
                json={},
            )
            return r.status_code in (200, 204)

    async def void_envelope(self, envelope_id: str, reason: str = "Cancelled by LexAI") -> bool:
        base = _api_base()
        url = f"{base}/v2.1/accounts/{self.account_id}/envelopes/{envelope_id}"
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.put(
                url,
                headers=self._headers(),
                json={"status": "voided", "voidedReason": reason},
            )
            return r.status_code in (200, 204)

    async def download_combined_pdf(self, envelope_id: str) -> Optional[bytes]:
        """Descarga el PDF combinado firmado · 'combined' incluye cert + audit."""
        base = _api_base()
        url = f"{base}/v2.1/accounts/{self.account_id}/envelopes/{envelope_id}/documents/combined"
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.get(
                url,
                headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/pdf"},
            )
            if r.status_code != 200:
                return None
            return r.content
