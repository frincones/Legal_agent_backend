"""Sprint 26 · Email provider abstraction.

Soporta:
  - mock  (default · no envía, solo loggea + retorna OK)
  - resend (https://resend.com)  · usa RESEND_API_KEY
  - postmark · usa POSTMARK_SERVER_TOKEN
  - smtp (genérico) · usa SMTP_* env vars

Selección por env LEXAI_EMAIL_PROVIDER (default 'mock').

API:
  send_email(to, subject, html, text=None, kind=None) → dict
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


def _provider() -> str:
    return os.getenv("LEXAI_EMAIL_PROVIDER", "mock").lower()


def _from_address() -> str:
    return os.getenv("LEXAI_EMAIL_FROM", "LexAI <hello@lexai.co>")


async def send_email(
    to: str,
    subject: str,
    html: str,
    text: Optional[str] = None,
    kind: Optional[str] = None,
) -> dict[str, Any]:
    """Envía un email. Retorna { ok, provider, message_id?, error? }."""
    provider = _provider()
    if provider == "mock":
        logger.info("email.mock: to=%s subject=%s kind=%s (NOT SENT · mock provider)", to, subject, kind)
        return {"ok": True, "provider": "mock", "message_id": f"mock_{kind}_{to}"}

    if provider == "resend":
        api_key = os.getenv("RESEND_API_KEY")
        if not api_key:
            return {"ok": False, "provider": "resend", "error": "RESEND_API_KEY not set"}
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "from": _from_address(),
                        "to": [to],
                        "subject": subject,
                        "html": html,
                        "text": text or _strip_html(html),
                    },
                )
                if r.status_code in (200, 201):
                    data = r.json()
                    return {"ok": True, "provider": "resend", "message_id": data.get("id")}
                return {"ok": False, "provider": "resend", "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        except Exception as e:
            return {"ok": False, "provider": "resend", "error": str(e)[:200]}

    if provider == "postmark":
        api_key = os.getenv("POSTMARK_SERVER_TOKEN")
        if not api_key:
            return {"ok": False, "provider": "postmark", "error": "POSTMARK_SERVER_TOKEN not set"}
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(
                    "https://api.postmarkapp.com/email",
                    headers={
                        "X-Postmark-Server-Token": api_key,
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    json={
                        "From": _from_address(),
                        "To": to,
                        "Subject": subject,
                        "HtmlBody": html,
                        "TextBody": text or _strip_html(html),
                        "MessageStream": "outbound",
                    },
                )
                if r.status_code == 200:
                    data = r.json()
                    return {"ok": True, "provider": "postmark", "message_id": data.get("MessageID")}
                return {"ok": False, "provider": "postmark", "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        except Exception as e:
            return {"ok": False, "provider": "postmark", "error": str(e)[:200]}

    if provider == "smtp":
        try:
            host = os.getenv("SMTP_HOST", "localhost")
            port = int(os.getenv("SMTP_PORT", "587"))
            user = os.getenv("SMTP_USER")
            password = os.getenv("SMTP_PASS")
            msg = MIMEMultipart("alternative")
            msg["From"] = _from_address()
            msg["To"] = to
            msg["Subject"] = subject
            if text:
                msg.attach(MIMEText(text, "plain", "utf-8"))
            msg.attach(MIMEText(html, "html", "utf-8"))
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.starttls()
                if user and password:
                    s.login(user, password)
                s.send_message(msg)
            return {"ok": True, "provider": "smtp"}
        except Exception as e:
            return {"ok": False, "provider": "smtp", "error": str(e)[:200]}

    return {"ok": False, "provider": provider, "error": "unknown provider"}


def _strip_html(html: str) -> str:
    """Plain-text fallback simple sin lib externa."""
    import re
    text = re.sub(r"<[^>]+>", "", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
