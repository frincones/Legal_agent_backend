"""Sprint 19 · Public intake form API.

ENDPOINTS PÚBLICOS · sin auth.

  GET  /v1/public/intake/{slug}     · render del form (sólo campos seguros)
  POST /v1/public/intake/{slug}     · submit del form

El backend valida payload + honeypot + rate limit (soft, in-memory).
Si pasa todo, crea fila en intake_submissions. El trigger SQL emite
activity_event para que el staff lo vea en el feed de actividad.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/public/intake", tags=["intake_public"])


# Rate limit en memoria · soft, lo reseteamos al restart. Para producción
# se debería usar Redis o algo persistente, pero para demo basta.
_RL: dict[str, deque] = {}
_RL_WINDOW = 3600  # 1 hora


def _check_rate_limit(ip: str, limit: int) -> bool:
    now = time.time()
    bucket = _RL.setdefault(ip, deque())
    cutoff = now - _RL_WINDOW
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


@router.get("/{slug}")
async def get_form_by_slug(slug: str):
    """Devuelve sólo campos seguros para renderizar el form en página pública."""
    slug = (slug or "").strip().lower()
    if not slug or len(slug) > 80:
        raise HTTPException(404, "Form no encontrado")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            "select * from lexai_intake_form_by_slug($1)", slug,
        )
        # Para mostrar logo + nombre del despacho
        firm_name = None
        if row:
            firm_name_row = await conn.fetchrow(
                "select razon_social from firms where id = $1::uuid", row["firm_id"],
            )
            if firm_name_row:
                firm_name = firm_name_row["razon_social"]
    if not row:
        raise HTTPException(404, "Form no encontrado o inactivo")

    # NO devolvemos firm_id ni honeypot_field (se mantiene secret en backend)
    return {
        "id": str(row["id"]),
        "slug": row["slug"],
        "name": row["name"],
        "description": row["description"],
        "fields": row["fields"] if not isinstance(row["fields"], str) else (json.loads(row["fields"]) if row["fields"] else []),
        "thank_you_message": row["thank_you_message"],
        "redirect_url": row["redirect_url"],
        "brand_color": row["brand_color"],
        "show_firm_logo": bool(row["show_firm_logo"]),
        "firm_name": firm_name,
    }


@router.post("/{slug}", status_code=201)
async def submit_form(slug: str, request: Request):
    slug = (slug or "").strip().lower()
    if not slug or len(slug) > 80:
        raise HTTPException(404, "Form no encontrado")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Payload JSON inválido")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Payload debe ser objeto JSON")

    # Rate limit por IP (best effort · X-Forwarded-For)
    client_ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )

    from utils.db import get_storage
    from utils.intake_validators import validate_submission
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")

    async with storage.pool.acquire() as conn:
        form = await conn.fetchrow(
            "select * from lexai_intake_form_by_slug($1)", slug,
        )
        if not form:
            raise HTTPException(404, "Form no encontrado o inactivo")
        # Rate limit check
        rl = int(_get(form, "rate_limit_per_ip", default=None) or 0)
        # default 5/h si no está en RPC (lexai_intake_form_by_slug no lo devuelve)
        if rl <= 0:
            rl = 5
        if not _check_rate_limit(client_ip, rl):
            raise HTTPException(429, "Demasiados intentos. Intenta más tarde.")

        fields = form["fields"] if not isinstance(form["fields"], str) else (json.loads(form["fields"]) if form["fields"] else [])
        honeypot_field = form["honeypot_field"]

        result = validate_submission(fields, payload, honeypot_field=honeypot_field)
        if result["errors"]:
            raise HTTPException(400, {"detail": "Errores de validación", "errors": result["errors"]})

        ua = request.headers.get("user-agent", "")[:500]
        referer = request.headers.get("referer", "")[:500]
        status = "spam" if result["is_spam"] else "new"

        row = await conn.fetchrow(
            """
            insert into intake_submissions
              (firm_id, intake_form_id, payload, submitter_email,
               submitter_nombre, submitter_phone, status,
               ip_address, user_agent, referer)
            values ($1::uuid, $2::uuid, $3::jsonb, $4, $5, $6, $7, $8::inet, $9, $10)
            returning id, created_at
            """,
            form["firm_id"], form["id"], json.dumps(result["cleaned"]),
            result["submitter_email"], result["submitter_nombre"], result["submitter_phone"],
            status, client_ip if client_ip != "unknown" else None,
            ua, referer,
        )

    if status == "spam":
        # Mostramos thank-you al bot también, pero no creamos lead.
        return {
            "ok": True,
            "submission_id": str(row["id"]),
            "thank_you_message": form["thank_you_message"],
        }

    return {
        "ok": True,
        "submission_id": str(row["id"]),
        "thank_you_message": form["thank_you_message"],
        "redirect_url": form["redirect_url"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


def _get(record, key: str, default=None):
    """Tolerante para asyncpg.Record · si la columna no está, devuelve default."""
    try:
        return record[key]
    except (KeyError, IndexError, TypeError):
        return default
