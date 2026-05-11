"""Sprint 5 · daily_briefing tool.

Genera un briefing matutino para un abogado:
  - Plazos próximos (matter_deadlines pendientes < 7 días)
  - Notificaciones judiciales sin leer
  - Correos legales sin leer (críticos / altos)
  - Casos con riesgos de cambio normativo (legal_alerts)
  - Acciones recomendadas top-3

Output: dict estructurado + resumen narrativo (3 párrafos) listo para TTS.

Voz típica: "LexAI, dame mi briefing del día".
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """Eres un asistente jurídico colombiano que da briefings concisos
a un abogado. Tono: profesional, directo, español de Colombia.
Estructura el briefing en 3 párrafos cortos:

1. URGENCIAS (plazos < 48h, notificaciones críticas)
2. NOVEDADES (correos legales nuevos, alertas normativas)
3. RECOMENDACIONES (top 3 acciones para hoy)

Si no hay urgencias, dilo expresamente. NO inventes casos.
Devuelve texto plano listo para TTS (sin markdown, sin headers)."""


async def daily_briefing_tool(args: dict, ctx: dict) -> dict:
    """Voice tool: 'LexAI, dame mi briefing del día'."""
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id") or args.get("user_id")
    if not firm_id:
        return {"error": "firm_id requerido"}

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}

    today = date.today()
    in_7_days = today + timedelta(days=7)
    in_2_days = today + timedelta(days=2)

    async with storage.pool.acquire() as conn:
        plazos_proximos = await conn.fetch(
            """
            select md.id, md.titulo, md.fecha, md.tipo, md.matter_id,
                   m.titulo as matter_titulo
              from matter_deadlines md
              left join matters m on m.id = md.matter_id
             where md.firm_id = $1::uuid
               and md.completado = false
               and md.fecha between $2::date and $3::date
             order by md.fecha asc
             limit 10
            """,
            firm_id, today, in_7_days,
        )
        plazos_criticos = [p for p in plazos_proximos if p["fecha"] and p["fecha"] <= in_2_days]

        notif_criticas = await conn.fetch(
            """
            select id, titulo, severidad, fecha_publicacion, expediente,
                   juzgado, matter_id
              from judicial_notifications
             where firm_id = $1::uuid and status = 'unread'
               and severidad in ('critica','alta')
             order by case severidad when 'critica' then 1 else 2 end, created_at desc
             limit 10
            """,
            firm_id,
        )
        emails_nuevos = await conn.fetch(
            """
            select id, subject, severidad, parsed_summary, from_address,
                   matched_expediente
              from email_messages
             where firm_id = $1::uuid and is_legal = true and status = 'unread'
             order by case severidad when 'critica' then 1 when 'alta' then 2 else 3 end,
                      received_at desc
             limit 10
            """,
            firm_id,
        )
        alertas_norm = await conn.fetch(
            """
            select id, title, description, severity, target_ref
              from legal_alerts
             where firm_id = $1::uuid
               and read_at is null and dismissed_at is null
             order by detected_at desc limit 10
            """,
            firm_id,
        )

    structured = {
        "fecha": today.isoformat(),
        "plazos_proximos": [
            {
                "id": str(p["id"]),
                "titulo": p["titulo"],
                "fecha": p["fecha"].isoformat() if p["fecha"] else None,
                "tipo": p["tipo"],
                "matter_id": str(p["matter_id"]) if p["matter_id"] else None,
                "matter_titulo": p["matter_titulo"],
                "dias_restantes": (p["fecha"] - today).days if p["fecha"] else None,
            }
            for p in plazos_proximos
        ],
        "plazos_criticos_count": len(plazos_criticos),
        "judicial_unread": [
            {
                "id": str(n["id"]),
                "titulo": n["titulo"],
                "severidad": n["severidad"],
                "expediente": n["expediente"],
                "juzgado": n["juzgado"],
            }
            for n in notif_criticas
        ],
        "emails_unread": [
            {
                "id": str(e["id"]),
                "subject": e["subject"],
                "severidad": e["severidad"],
                "summary": e["parsed_summary"],
                "from": e["from_address"],
                "expediente": e["matched_expediente"],
            }
            for e in emails_nuevos
        ],
        "alertas_normativas": [
            {
                "id": str(a["id"]),
                "title": a["title"],
                "severity": a["severity"],
                "target_ref": a["target_ref"],
            }
            for a in alertas_norm
        ],
    }

    # Generar resumen narrativo con LLM (best-effort)
    narrative = _fallback_narrative(structured)
    try:
        from utils.llm import get_openai_client
        client = get_openai_client()
        user_prompt = (
            f"Genera el briefing del día {today.isoformat()} basado en estos datos:\n\n"
            f"URGENCIAS:\n"
            f"- Plazos críticos (<48h): {len(plazos_criticos)}\n"
            f"- Notificaciones judiciales unread: {len(notif_criticas)}\n\n"
            f"NOVEDADES:\n"
            f"- Correos legales nuevos: {len(emails_nuevos)}\n"
            f"- Alertas normativas: {len(alertas_norm)}\n\n"
            f"PLAZOS:\n" + "\n".join(
                f"- {p['titulo']} ({p['matter_titulo'] or 'sin caso'}) "
                f"vence {p['fecha'].isoformat()} en {(p['fecha'] - today).days} días"
                for p in plazos_proximos[:5] if p["fecha"]
            ) + "\n\n"
            f"NOTIFICACIONES:\n" + "\n".join(
                f"- [{n['severidad']}] {n['titulo']}" for n in notif_criticas[:5]
            ) + "\n"
        )
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=500,
        )
        narrative = (resp.choices[0].message.content or narrative).strip()
    except Exception as e:
        logger.warning("daily_briefing LLM failed: %s", e)

    structured["narrative"] = narrative
    return structured


def _fallback_narrative(data: dict) -> str:
    """Narrativa por reglas si el LLM falla."""
    parts: list[str] = []
    n_plazos = len(data["plazos_proximos"])
    n_judicial = len(data["judicial_unread"])
    n_emails = len(data["emails_unread"])
    n_alertas = len(data["alertas_normativas"])
    if data["plazos_criticos_count"] > 0:
        parts.append(
            f"Tienes {data['plazos_criticos_count']} plazo"
            f"{'s' if data['plazos_criticos_count'] != 1 else ''} crítico"
            f"{'s' if data['plazos_criticos_count'] != 1 else ''} en las próximas 48 horas."
        )
    elif n_plazos > 0:
        parts.append(f"Tienes {n_plazos} plazo{'s' if n_plazos != 1 else ''} en los próximos 7 días.")
    else:
        parts.append("No hay plazos urgentes esta semana.")
    if n_judicial > 0 or n_emails > 0:
        parts.append(
            f"Llegaron {n_judicial} notificación"
            f"{'es' if n_judicial != 1 else ''} judicial"
            f"{'es' if n_judicial != 1 else ''} y {n_emails} correo"
            f"{'s' if n_emails != 1 else ''} legal"
            f"{'es' if n_emails != 1 else ''} sin revisar."
        )
    if n_alertas > 0:
        parts.append(f"Hay {n_alertas} cambio{'s' if n_alertas != 1 else ''} normativo que pueden afectar tus casos.")
    if data["plazos_criticos_count"] > 0:
        parts.append("Recomiendo: revisar plazos críticos, leer notificaciones de juzgado, atender correos urgentes.")
    return " ".join(parts)
