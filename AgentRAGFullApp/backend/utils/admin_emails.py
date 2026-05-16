"""Sprint F · Email notifications para acciones admin SaaS.

Soporte multi-provider · prioridad:
  1. Resend (RESEND_API_KEY) · preferido
  2. SendGrid (SENDGRID_API_KEY)
  3. SMTP (SMTP_HOST, SMTP_USER, SMTP_PASS, SMTP_PORT) · fallback
  4. Si nada está configurado → log warning + retorna ok=False (non-blocking)

Templates en HTML inline · sin dependencias externas.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


FROM_DEFAULT = os.getenv("LEXAI_EMAILS_FROM", "LexAI <noreply@lexai.co>")


async def send_email(
    *,
    to: str,
    subject: str,
    html: str,
    text: Optional[str] = None,
    from_addr: Optional[str] = None,
) -> dict:
    """Envía email vía Resend > SendGrid > SMTP > noop.
    Retorna {"ok": bool, "provider": str, "error": str|None}."""
    sender = from_addr or FROM_DEFAULT

    # 1) Resend
    if os.getenv("RESEND_API_KEY"):
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {os.getenv('RESEND_API_KEY')}",
                        "Content-Type": "application/json",
                    },
                    json={"from": sender, "to": to, "subject": subject,
                            "html": html, "text": text or _html_to_text(html)},
                )
                if r.status_code in (200, 201, 202):
                    return {"ok": True, "provider": "resend", "id": r.json().get("id")}
                return {"ok": False, "provider": "resend",
                        "error": f"{r.status_code}: {r.text[:120]}"}
        except Exception as e:
            logger.warning("Resend send failed: %s", e)

    # 2) SendGrid
    if os.getenv("SENDGRID_API_KEY"):
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.post(
                    "https://api.sendgrid.com/v3/mail/send",
                    headers={
                        "Authorization": f"Bearer {os.getenv('SENDGRID_API_KEY')}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "from": {"email": _strip_brackets(sender)},
                        "personalizations": [{"to": [{"email": to}], "subject": subject}],
                        "content": [
                            {"type": "text/html", "value": html},
                            {"type": "text/plain", "value": text or _html_to_text(html)},
                        ],
                    },
                )
                return {"ok": r.status_code in (200, 202),
                        "provider": "sendgrid",
                        "error": None if r.status_code == 202 else f"{r.status_code}"}
        except Exception as e:
            logger.warning("SendGrid send failed: %s", e)

    # 3) SMTP
    smtp_host = os.getenv("SMTP_HOST")
    if smtp_host:
        try:
            import smtplib
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart
            msg = MIMEMultipart("alternative")
            msg["From"] = sender
            msg["To"] = to
            msg["Subject"] = subject
            msg.attach(MIMEText(text or _html_to_text(html), "plain"))
            msg.attach(MIMEText(html, "html"))
            with smtplib.SMTP_SSL(smtp_host, int(os.getenv("SMTP_PORT", "465"))) as srv:
                srv.login(os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASS", ""))
                srv.send_message(msg)
            return {"ok": True, "provider": "smtp"}
        except Exception as e:
            logger.warning("SMTP send failed: %s", e)

    # 4) Noop
    logger.info("Email not sent · no provider configured · to=%s subject=%s",
                  to, subject)
    return {"ok": False, "provider": "noop", "error": "no_provider_configured"}


def _html_to_text(html: str) -> str:
    import re
    return re.sub(r"<[^>]+>", " ", html).strip()


def _strip_brackets(addr: str) -> str:
    # "LexAI <x@y.com>" → "x@y.com"
    import re
    m = re.search(r"<([^>]+)>", addr)
    return m.group(1) if m else addr


# ─────────────────────────────────────────────────────────────────────
# Template · plan change notification
# ─────────────────────────────────────────────────────────────────────

async def send_plan_change_notification(
    *,
    to_email: str,
    firm_name: str,
    old_plan_name: str,
    new_plan_name: str,
    new_plan_monthly_cop: int,
    billing_period: str,
    reason: str,
    paddle_synced: bool,
    next_billed_at: Optional[str] = None,
    admin_email: str = "soporte@lexai.co",
) -> dict:
    """Notifica al admin de la firma que su plan fue cambiado."""
    subject = f"Tu plan en LexAI cambió a {new_plan_name}"
    monthly_cop_str = f"${new_plan_monthly_cop:,} COP".replace(",", ".")
    period_text = "mensual" if billing_period == "monthly" else "anual"

    html = f"""\
<!DOCTYPE html>
<html lang="es">
<head><meta charset="utf-8"><title>{subject}</title></head>
<body style="font-family:Inter,Helvetica,Arial,sans-serif;background:#f7f7f5;padding:24px;color:#1d1d1f;line-height:1.5">
  <div style="max-width:560px;margin:0 auto;background:#fff;border:1px solid #e5e5e3;border-radius:8px;overflow:hidden">
    <div style="padding:24px 28px;border-bottom:1px solid #e5e5e3;background:linear-gradient(180deg,#fafaf9,#fff)">
      <h1 style="margin:0;font-size:20px;color:#1d1d1f">LexAI · Cambio de plan</h1>
      <p style="margin:6px 0 0;font-size:13px;color:#6b6b66">Notificación administrativa</p>
    </div>
    <div style="padding:24px 28px">
      <p style="margin:0 0 16px">Hola,</p>
      <p style="margin:0 0 16px">Tu despacho <strong>{firm_name}</strong> tiene un nuevo plan activo en LexAI:</p>
      <table style="width:100%;border-collapse:collapse;margin:16px 0">
        <tr>
          <td style="padding:8px 12px;background:#f7f7f5;border-radius:4px;width:50%">
            <div style="font-size:11px;color:#6b6b66;text-transform:uppercase;letter-spacing:.5px">Plan anterior</div>
            <div style="font-size:16px;color:#1d1d1f;text-decoration:line-through">{old_plan_name}</div>
          </td>
          <td style="padding:8px 12px;background:#eef9ef;border-radius:4px">
            <div style="font-size:11px;color:#2c7a3d;text-transform:uppercase;letter-spacing:.5px">Plan actual</div>
            <div style="font-size:16px;color:#1d1d1f;font-weight:600">{new_plan_name}</div>
            <div style="font-size:12px;color:#6b6b66">{monthly_cop_str} {period_text}</div>
          </td>
        </tr>
      </table>
      <p style="margin:0 0 12px;font-size:13px">
        <strong>Razón del cambio:</strong><br>
        <span style="color:#6b6b66">{_escape_html(reason)}</span>
      </p>
"""
    if paddle_synced:
        html += f"""
      <div style="background:#fff8e6;border-left:4px solid #f5a623;padding:12px 16px;margin:16px 0;border-radius:4px">
        <strong style="font-size:13px;color:#a05a00">Cobro sincronizado</strong>
        <p style="margin:4px 0 0;font-size:12.5px;color:#6b5a00">
          Tu suscripción de Paddle se actualizó automáticamente. El costo del nuevo plan
          ({monthly_cop_str}) se cobrará en breve y el ciclo se reiniciará. Próximo cobro: {next_billed_at or 'próximo mes'}.
        </p>
      </div>
"""

    html += f"""
      <p style="margin:24px 0 0;font-size:13px;color:#6b6b66">
        Si crees que esto es un error o necesitas más información, responde a este email
        o contacta a <a href="mailto:{admin_email}" style="color:#1d1d1f">{admin_email}</a>.
      </p>
    </div>
    <div style="padding:16px 28px;background:#fafaf9;border-top:1px solid #e5e5e3;font-size:11.5px;color:#888">
      Este es un mensaje automatizado del panel administrativo de LexAI.
      Cambio realizado por el equipo de soporte SaaS.
    </div>
  </div>
</body>
</html>
"""
    return await send_email(to=to_email, subject=subject, html=html)


def _escape_html(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))
