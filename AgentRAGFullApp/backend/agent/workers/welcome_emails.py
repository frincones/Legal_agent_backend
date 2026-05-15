"""Sprint 26 · Welcome emails worker.

Envía 4 emails de activación en cadencia:
  - D0 (immediate · al signup) · welcome_d0
  - D1 (24h después)           · tip_d1 · "¿cómo va tu primer caso?"
  - D3 (72h después)           · court_watcher_d3 · feature push
  - D7 (semana)                · trial_status_d7 · "tu trial termina en X días"

Punto de entrada:
  POST /v1/admin/welcome-emails/run     · corre worker (cron Railway lo invoca cada hora)
  POST /v1/admin/welcome-emails/send    · admin · envío manual a un firm específico

Idempotencia: welcome_emails_log tiene UNIQUE (firm_id, kind) → no duplica.

Provider: configurado vía LEXAI_EMAIL_PROVIDER · 'mock' por defecto (no envía).
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Templates HTML inline (minimalist · sin styling externo)
# ──────────────────────────────────────────────────────────────────────


def _layout(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="utf-8"><title>{title}</title></head>
<body style="font-family: -apple-system, system-ui, sans-serif; max-width: 580px; margin: 0 auto; padding: 32px 24px; color: #1a1a1a; line-height: 1.5;">
  <div style="margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid #eee;">
    <strong style="font-size: 18px; color: #2563eb;">LexAI</strong>
    <span style="font-size: 11px; color: #888; text-transform: uppercase; letter-spacing: 1px; margin-left: 8px;">Plataforma legal con IA</span>
  </div>
  {body}
  <div style="margin-top: 32px; padding-top: 16px; border-top: 1px solid #eee; font-size: 11px; color: #888;">
    <p>© 2026 LexAI · Asistencia documental con IA · No constituye representación legal.</p>
    <p>Recibiste este email porque te registraste en LexAI. Si no fuiste tú, ignora este mensaje.</p>
  </div>
</body>
</html>"""


def template_welcome_d0(user_name: str, firm_name: str) -> tuple[str, str]:
    subject = "¡Bienvenido a LexAI!"
    body = f"""
<h1 style="font-size: 22px; margin-bottom: 8px;">Hola {user_name},</h1>
<p>Gracias por registrar <strong>{firm_name}</strong> en LexAI.</p>
<p>Tu cuenta está lista. Te sembramos 2 clientes y 2 casos de ejemplo para que explores la plataforma sin partir de cero.</p>
<p style="margin: 24px 0;">
  <a href="https://lexai-frontend-rho.vercel.app/inicio?welcome=1"
     style="display: inline-block; background: #2563eb; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: 500;">
    Empezar →
  </a>
</p>
<p style="font-size: 13px; color: #666;">
  Tienes <strong>14 días gratis</strong> en tu trial. Sin tarjeta de crédito. Cancelas cuando quieras.
</p>
<h3 style="font-size: 15px; margin-top: 24px;">3 cosas que puedes probar ahora:</h3>
<ul style="font-size: 13px;">
  <li>📁 Abrir el caso DEMO-001 y explorar análisis IA + cronología</li>
  <li>💬 Decirle al asistente de voz: "abre mis casos urgentes"</li>
  <li>⚖️ Calcular una liquidación laboral en 30 segundos</li>
</ul>
"""
    return subject, _layout(subject, body)


def template_tip_d1(user_name: str) -> tuple[str, str]:
    subject = "¿Cómo va tu primer caso en LexAI?"
    body = f"""
<h1 style="font-size: 20px; margin-bottom: 8px;">Hola {user_name},</h1>
<p>Hace 24h te diste de alta en LexAI. ¿Has explorado?</p>
<p>Lo que más rápido te dará valor: <strong>arrastrar un documento real</strong> a la pantalla "Documentos". LexAI lo procesa con OCR + resumen IA en segundos.</p>
<p style="margin: 24px 0;">
  <a href="https://lexai-frontend-rho.vercel.app/documentos"
     style="display: inline-block; background: #2563eb; color: white; padding: 10px 20px; text-decoration: none; border-radius: 6px;">
    Subir documento →
  </a>
</p>
<p style="font-size: 12px; color: #666;">Si necesitas ayuda, responde este email o haz click en el botón <strong>?</strong> dentro de la app.</p>
"""
    return subject, _layout(subject, body)


def template_court_watcher_d3(user_name: str) -> tuple[str, str]:
    subject = "💡 La feature que nadie más tiene · Court Watcher"
    body = f"""
<h1 style="font-size: 20px; margin-bottom: 8px;">Hola {user_name},</h1>
<p>La mayoría de los abogados visitan el juzgado <strong>cada semana</strong> para revisar el pizarrón de audiencias.</p>
<p>Con <strong>Court Watcher</strong> de LexAI, esa visita se acaba: nuestro robot monitorea los portales judiciales y te avisa apenas cambia algo en tus expedientes.</p>
<ul style="font-size: 13px;">
  <li>🔔 Audiencias programadas y cambios de juez</li>
  <li>📝 Notificaciones nuevas en tus casos activos</li>
  <li>⚖️ Cambios normativos que afectan tus expedientes</li>
</ul>
<p style="margin: 24px 0;">
  <a href="https://lexai-frontend-rho.vercel.app/settings/billing"
     style="display: inline-block; background: #2563eb; color: white; padding: 10px 20px; text-decoration: none; border-radius: 6px;">
    Ver planes con Court Watcher →
  </a>
</p>
<p style="font-size: 12px; color: #666;">Court Watcher está disponible desde el plan Pro ($149k/mes).</p>
"""
    return subject, _layout(subject, body)


def template_trial_d7(user_name: str, days_left: int) -> tuple[str, str]:
    subject = f"Tu trial termina en {days_left} días"
    body = f"""
<h1 style="font-size: 20px; margin-bottom: 8px;">Hola {user_name},</h1>
<p>Tu trial gratis de LexAI termina en <strong>{days_left} días</strong>.</p>
<p>Si quieres seguir con todas las funcionalidades premium (Canvas, Court Watcher, jueces, simulador, etc.), activa un plan:</p>
<p style="margin: 24px 0;">
  <a href="https://lexai-frontend-rho.vercel.app/settings/billing"
     style="display: inline-block; background: #2563eb; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: 500;">
    Elegir mi plan →
  </a>
</p>
<p style="font-size: 13px; color: #666;">
  Si no haces nada, tu cuenta pasará automáticamente al plan Free (gratis, con límites). No te cobramos sin tu autorización.
</p>
"""
    return subject, _layout(subject, body)


# ──────────────────────────────────────────────────────────────────────
# Worker logic
# ──────────────────────────────────────────────────────────────────────


async def _already_sent(conn, firm_id: str, kind: str) -> bool:
    return bool(await conn.fetchval(
        "select exists (select 1 from welcome_emails_log where firm_id = $1::uuid and kind = $2)",
        firm_id, kind,
    ))


async def _log_send(conn, firm_id: str, user_id: str, email: str, kind: str, result: dict) -> None:
    await conn.execute(
        """
        insert into welcome_emails_log
          (firm_id, user_id, recipient_email, kind, provider, provider_message_id, metadata)
        values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7::jsonb)
        on conflict (firm_id, kind) do nothing
        """,
        firm_id, user_id, email, kind,
        result.get("provider", "unknown"),
        result.get("message_id"),
        json.dumps({"ok": result.get("ok", False), "error": result.get("error")}),
    )


async def run_welcome_emails() -> dict[str, Any]:
    """Itera firms y envía los emails que correspondan según la edad."""
    from utils.db import get_storage
    from utils.email_provider import send_email

    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"ok": False, "error": "no_storage"}

    sent_counts = {"welcome_d0": 0, "tip_d1": 0, "court_watcher_d3": 0, "trial_status_d7": 0}
    errors = []

    async with storage.pool.acquire() as conn:
        # Firms candidatos (no enterprise · solo trial)
        firms = await conn.fetch(
            """
            select f.id as firm_id, f.razon_social, f.created_at,
                   u.id as user_id, u.email, u.full_name,
                   s.status as sub_status, s.trial_ends_at,
                   extract(epoch from (now() - f.created_at)) / 3600 as hours_since_signup
              from firms f
              left join firm_subscriptions s on s.firm_id = f.id
              left join users u on u.firm_id = f.id and u.role in ('admin','socio_senior','owner','independiente','in_house')
             where u.email is not null
            """
        )

        for firm in firms:
            firm_id = str(firm["firm_id"])
            user_id = str(firm["user_id"])
            email = firm["email"]
            user_name = (firm["full_name"] or email.split("@")[0]).split()[0]
            firm_name = firm["razon_social"]
            hours = float(firm["hours_since_signup"] or 0)
            trial_ends = firm["trial_ends_at"]

            # D0 · primera hora del signup
            if hours <= 2 and not await _already_sent(conn, firm_id, "welcome_d0"):
                subject, html = template_welcome_d0(user_name, firm_name)
                result = await send_email(email, subject, html, kind="welcome_d0")
                await _log_send(conn, firm_id, user_id, email, "welcome_d0", result)
                sent_counts["welcome_d0"] += 1

            # D1 · entre 24-30h del signup
            elif 24 <= hours <= 30 and not await _already_sent(conn, firm_id, "tip_d1"):
                subject, html = template_tip_d1(user_name)
                result = await send_email(email, subject, html, kind="tip_d1")
                await _log_send(conn, firm_id, user_id, email, "tip_d1", result)
                sent_counts["tip_d1"] += 1

            # D3 · entre 72-78h
            elif 72 <= hours <= 78 and not await _already_sent(conn, firm_id, "court_watcher_d3"):
                subject, html = template_court_watcher_d3(user_name)
                result = await send_email(email, subject, html, kind="court_watcher_d3")
                await _log_send(conn, firm_id, user_id, email, "court_watcher_d3", result)
                sent_counts["court_watcher_d3"] += 1

            # D7 · trial casi vence (entre 7 y 1 días para vencer)
            elif trial_ends and not await _already_sent(conn, firm_id, "trial_status_d7"):
                from datetime import datetime, timezone
                days_left = (trial_ends - datetime.now(timezone.utc)).days
                if 1 <= days_left <= 7:
                    subject, html = template_trial_d7(user_name, days_left)
                    result = await send_email(email, subject, html, kind="trial_status_d7")
                    await _log_send(conn, firm_id, user_id, email, "trial_status_d7", result)
                    sent_counts["trial_status_d7"] += 1

    return {"ok": True, "sent": sent_counts, "errors": errors}
