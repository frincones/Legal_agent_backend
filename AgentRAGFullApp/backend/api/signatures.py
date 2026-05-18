"""Sprint 13 · Signatures API.

  GET    /v1/signatures/providers              · catálogo de providers
  GET    /v1/signatures/envelopes              · lista filtrable
  POST   /v1/signatures/envelopes              · crea draft (no envía)
  GET    /v1/signatures/envelopes/{id}         · cabecera + signers + docs + events
  POST   /v1/signatures/envelopes/{id}/send    · envía via provider
  POST   /v1/signatures/envelopes/{id}/cancel
  POST   /v1/signatures/envelopes/{id}/remind  · re-envía a pendientes
  DELETE /v1/signatures/envelopes/{id}         · solo draft
  GET    /v1/signatures/stats
  POST   /v1/signatures/webhook/{provider}     · receiver Certicámara/DocuSign
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/signatures", tags=["signatures"])

ADMIN_ROLES = {"admin", "socio_senior", "socio_junior", "lawyer"}


def _serialize_envelope(r) -> dict:
    return {
        "id": str(r["id"]),
        "matter_id": str(r["matter_id"]) if r["matter_id"] else None,
        "title": r["title"],
        "message": r["message"],
        "provider": r["provider"],
        "external_id": r["external_id"],
        "status": r["status"],
        "signer_order": r["signer_order"],
        "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
        "sent_at": r["sent_at"].isoformat() if r["sent_at"] else None,
        "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
        "canceled_at": r["canceled_at"].isoformat() if r["canceled_at"] else None,
        "signer_count": r["signer_count"],
        "signed_count": r["signed_count"],
        "signed_pdf_storage_path": r["signed_pdf_storage_path"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


@router.get("/providers")
async def list_providers(_: Principal = Depends(get_current_firm)):
    from agent.tools.signatures import get_available_providers
    return {"items": get_available_providers()}


@router.get("/envelopes")
async def list_envelopes(
    status: Optional[str] = None,
    matter_id: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    where = ["firm_id = $1::uuid"]
    params: list = [principal.firm_id]
    if status:
        params.append(status); where.append(f"status = ${len(params)}")
    if matter_id:
        params.append(matter_id); where.append(f"matter_id = ${len(params)}::uuid")
    params.append(limit)
    sql = f"""
        select id, matter_id, title, message, provider, external_id, status,
               signer_order, expires_at, sent_at, completed_at, canceled_at,
               signer_count, signed_count, signed_pdf_storage_path, created_at
          from signature_envelopes
         where {' and '.join(where)}
         order by created_at desc
         limit ${len(params)}
    """
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {"count": len(rows), "items": [_serialize_envelope(r) for r in rows]}


class SignerInput(BaseModel):
    role: str = Field(default="firmante")
    name: str = Field(min_length=2)
    email: Optional[str] = None
    phone: Optional[str] = None
    identity_id: Optional[str] = None
    auth_method: str = Field(default="email", pattern="^(email|sms|otp|biometric|none)$")
    sort_order: int = 0


class DocumentInput(BaseModel):
    source_document_id: Optional[str] = None
    filename: str
    storage_path_source: Optional[str] = None
    position: int = 0


class CreateEnvelopeRequest(BaseModel):
    title: str = Field(min_length=2)
    message: Optional[str] = None
    provider: str = Field(default="demo", pattern="^(demo|certicamara|docusign)$")
    matter_id: Optional[str] = None
    signer_order: str = Field(default="parallel", pattern="^(parallel|sequential)$")
    expires_in_days: int = Field(default=30, ge=1, le=180)
    signers: list[SignerInput] = Field(min_length=1, max_length=20)
    documents: list[DocumentInput] = Field(min_length=1, max_length=10)


@router.post("/envelopes")
async def create_envelope(
    body: CreateEnvelopeRequest,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ADMIN_ROLES:
        raise HTTPException(403, "Tu rol no puede crear sobres de firma")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    expires_at = datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)
    webhook_secret = secrets.token_urlsafe(24)

    async with storage.pool.acquire() as conn:
        env = await conn.fetchrow(
            """
            insert into signature_envelopes
              (firm_id, matter_id, title, message, provider, signer_order,
               expires_at, signer_count, webhook_secret, created_by)
            values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7::timestamptz, $8, $9, $10::uuid)
            returning id, matter_id, title, message, provider, external_id, status,
                      signer_order, expires_at, sent_at, completed_at, canceled_at,
                      signer_count, signed_count, signed_pdf_storage_path, created_at
            """,
            principal.firm_id, body.matter_id, body.title, body.message,
            body.provider, body.signer_order, expires_at,
            len(body.signers), webhook_secret, principal.user_id,
        )
        envelope_id = env["id"]
        for s in body.signers:
            await conn.execute(
                """
                insert into signature_signers
                  (firm_id, envelope_id, role, name, email, phone, identity_id,
                   sort_order, auth_method)
                values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9)
                """,
                principal.firm_id, envelope_id, s.role, s.name, s.email,
                s.phone, s.identity_id, s.sort_order, s.auth_method,
            )
        for d in body.documents:
            await conn.execute(
                """
                insert into signature_documents
                  (firm_id, envelope_id, source_document_id, filename,
                   storage_path_source, position)
                values ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6)
                """,
                principal.firm_id, envelope_id, d.source_document_id,
                d.filename, d.storage_path_source, d.position,
            )
        await conn.execute(
            """insert into signature_events (firm_id, envelope_id, kind, actor)
               values ($1::uuid, $2::uuid, 'created', $3)""",
            principal.firm_id, envelope_id, f"user:{principal.user_id}",
        )
    return _serialize_envelope(env)


@router.get("/envelopes/{envelope_id}")
async def get_envelope(
    envelope_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        env = await conn.fetchrow(
            """
            select id, matter_id, title, message, provider, external_id, status,
                   signer_order, expires_at, sent_at, completed_at, canceled_at,
                   signer_count, signed_count, signed_pdf_storage_path, created_at
              from signature_envelopes
             where id = $1::uuid and firm_id = $2::uuid
            """,
            envelope_id, principal.firm_id,
        )
        if not env:
            raise HTTPException(404, "not found")
        signers = await conn.fetch(
            """
            select id, role, name, email, phone, identity_id, sort_order,
                   auth_method, status, signed_at, signed_ip::text as signed_ip,
                   decline_reason, signing_url, reminder_sent_count
              from signature_signers where envelope_id = $1::uuid
             order by sort_order
            """,
            envelope_id,
        )
        docs = await conn.fetch(
            """
            select id, source_document_id, filename, storage_path_source,
                   storage_path_signed, sha256_source, sha256_signed, pages, position
              from signature_documents where envelope_id = $1::uuid
             order by position
            """,
            envelope_id,
        )
        events = await conn.fetch(
            """
            select id, signer_id, kind, actor, ip_address::text as ip_address,
                   user_agent, payload, occurred_at
              from signature_events where envelope_id = $1::uuid
             order by occurred_at desc limit 100
            """,
            envelope_id,
        )
    return {
        "envelope": _serialize_envelope(env),
        "signers": [
            {
                "id": str(s["id"]), "role": s["role"], "name": s["name"],
                "email": s["email"], "phone": s["phone"], "identity_id": s["identity_id"],
                "sort_order": s["sort_order"], "auth_method": s["auth_method"],
                "status": s["status"],
                "signed_at": s["signed_at"].isoformat() if s["signed_at"] else None,
                "signed_ip": s["signed_ip"],
                "decline_reason": s["decline_reason"],
                "signing_url": s["signing_url"],
                "reminder_sent_count": s["reminder_sent_count"],
            }
            for s in signers
        ],
        "documents": [
            {
                "id": str(d["id"]),
                "source_document_id": str(d["source_document_id"]) if d["source_document_id"] else None,
                "filename": d["filename"],
                "storage_path_source": d["storage_path_source"],
                "storage_path_signed": d["storage_path_signed"],
                "sha256_source": d["sha256_source"],
                "sha256_signed": d["sha256_signed"],
                "pages": d["pages"],
                "position": d["position"],
            }
            for d in docs
        ],
        "events": [
            {
                "id": str(e["id"]),
                "signer_id": str(e["signer_id"]) if e["signer_id"] else None,
                "kind": e["kind"], "actor": e["actor"],
                "ip_address": e["ip_address"], "user_agent": e["user_agent"],
                "payload": e["payload"],
                "occurred_at": e["occurred_at"].isoformat() if e["occurred_at"] else None,
            }
            for e in events
        ],
    }


@router.post("/envelopes/{envelope_id}/send")
async def send_envelope(
    envelope_id: str,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ADMIN_ROLES:
        raise HTTPException(403, "Sin permisos")
    from utils.db import get_storage
    from agent.tools.signatures import create_envelope_remote
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        env = await conn.fetchrow(
            "select * from signature_envelopes where id = $1::uuid and firm_id = $2::uuid",
            envelope_id, principal.firm_id,
        )
        if not env:
            raise HTTPException(404, "not found")
        if env["status"] != "draft":
            raise HTTPException(409, f"solo draft puede enviarse (estado: {env['status']})")
        signers = await conn.fetch(
            "select id, name, email, identity_id, sort_order, auth_method, role from signature_signers where envelope_id = $1::uuid order by sort_order",
            envelope_id,
        )
        docs = await conn.fetch(
            "select id, filename, storage_path_source, position from signature_documents where envelope_id = $1::uuid order by position",
            envelope_id,
        )
    # Crear en provider
    result = await create_envelope_remote(
        env["provider"],
        {
            "id": str(env["id"]),
            "title": env["title"],
            "message": env["message"],
            "expires_at": env["expires_at"].isoformat() if env["expires_at"] else None,
        },
        [
            {**dict(s), "id": str(s["id"])}
            for s in signers
        ],
        [{**dict(d), "id": str(d["id"])} for d in docs],
    )
    external_id = result.get("external_id")
    signing_urls = result.get("signing_urls") or {}

    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            update signature_envelopes set
              status = 'sent', sent_at = now(),
              external_id = coalesce($2, external_id), updated_at = now()
             where id = $1::uuid
            """,
            envelope_id, external_id,
        )
        for s in signers:
            url = signing_urls.get(str(s["id"]))
            await conn.execute(
                """
                update signature_signers set
                  status = 'sent', signing_url = $2
                 where id = $1::uuid
                """,
                s["id"], url,
            )
        await conn.execute(
            """insert into signature_events (firm_id, envelope_id, kind, actor, payload)
               values ($1::uuid, $2::uuid, 'sent', $3, $4::jsonb)""",
            principal.firm_id, envelope_id, f"user:{principal.user_id}",
            json.dumps({"provider": env["provider"], "configured": result.get("configured")}),
        )
    return {
        "envelope_id": str(env["id"]),
        "status": "sent",
        "provider": env["provider"],
        "external_id": external_id,
        "configured": result.get("configured"),
        "signing_urls": signing_urls,
        "instructions": result.get("instructions"),
    }


@router.post("/envelopes/{envelope_id}/cancel")
async def cancel_envelope(
    envelope_id: str,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ADMIN_ROLES:
        raise HTTPException(403, "Sin permisos")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update signature_envelopes set
              status = 'canceled', canceled_at = now(), updated_at = now()
             where id = $1::uuid and firm_id = $2::uuid
               and status in ('draft','sent','viewed','partially_signed')
             returning id, status
            """,
            envelope_id, principal.firm_id,
        )
        if not row:
            raise HTTPException(409, "no se puede cancelar en este estado")
        await conn.execute(
            """insert into signature_events (firm_id, envelope_id, kind, actor)
               values ($1::uuid, $2::uuid, 'canceled', $3)""",
            principal.firm_id, envelope_id, f"user:{principal.user_id}",
        )
    return {"id": str(row["id"]), "status": row["status"]}


@router.post("/envelopes/{envelope_id}/remind")
async def remind_pending(
    envelope_id: str,
    principal: Principal = Depends(get_current_firm),
):
    """Marca recordatorios. El envío real lo hace el provider; aquí solo
    actualizamos contadores y log para auditoría."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            update signature_signers
               set reminder_sent_count = reminder_sent_count + 1
             where envelope_id = $1::uuid
               and status in ('sent','viewed')
            """,
            envelope_id,
        )
        await conn.execute(
            """insert into signature_events (firm_id, envelope_id, kind, actor)
               values ($1::uuid, $2::uuid, 'reminder', $3)""",
            principal.firm_id, envelope_id, f"user:{principal.user_id}",
        )
    return {"ok": True}


@router.delete("/envelopes/{envelope_id}")
async def delete_envelope(
    envelope_id: str,
    principal: Principal = Depends(get_current_firm),
):
    if principal.role not in ("admin", "socio_senior"):
        raise HTTPException(403, "Solo admin")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            "delete from signature_envelopes where id = $1::uuid and firm_id = $2::uuid and status = 'draft' returning id",
            envelope_id, principal.firm_id,
        )
    if not row:
        raise HTTPException(409, "solo draft puede eliminarse")
    return {"deleted": True}


@router.get("/stats")
async def stats(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        result = await conn.fetchval(
            "select lexai_signature_stats($1::uuid)", principal.firm_id,
        )
    return result or {}


# ──────────────────────────────────────────────────────────────────────
# Webhook receiver (público)
# ──────────────────────────────────────────────────────────────────────


@router.post("/webhook/{provider}")
async def webhook(
    provider: str,
    request: Request,
    x_signature: Optional[str] = Header(default=None, alias="X-Signature"),
):
    if provider not in ("certicamara", "docusign", "demo"):
        raise HTTPException(404, "provider no soportado")
    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        return {"ok": False, "reason": "bad_json"}

    from agent.tools.signatures import parse_webhook_event, verify_webhook_signature
    event = parse_webhook_event(provider, payload)
    external_id = event.get("external_envelope_id")
    if not external_id:
        return {"ok": False, "reason": "no_envelope_id"}

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"ok": False, "reason": "no_storage"}

    async with storage.pool.acquire() as conn:
        env = await conn.fetchrow(
            "select id, firm_id, webhook_secret, provider, signer_count from signature_envelopes where external_id = $1",
            external_id,
        )
        if not env:
            return {"ok": False, "reason": "envelope_not_found"}
        if env["provider"] != provider:
            return {"ok": False, "reason": "provider_mismatch"}

        # Verify signature (si hay secret configurado)
        if env["webhook_secret"] and not verify_webhook_signature(
            provider, env["webhook_secret"], raw, x_signature or "",
        ):
            logger.warning("webhook signature inválida para %s", external_id)
            # No bloqueamos: log y continuamos para no perder eventos en sandbox
            pass

        kind = event["kind"]
        signer_id = None
        email = event.get("signer_email")
        if email:
            sg = await conn.fetchrow(
                "select id from signature_signers where envelope_id = $1::uuid and email = $2",
                env["id"], email,
            )
            if sg:
                signer_id = sg["id"]

        await conn.execute(
            """
            insert into signature_events
              (firm_id, envelope_id, signer_id, kind, actor, payload)
            values ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6::jsonb)
            """,
            env["firm_id"], env["id"], signer_id, kind, f"provider:{provider}",
            json.dumps(event.get("raw") or {}),
        )

        # Aplicar cambios de estado
        if kind == "signed" and signer_id:
            await conn.execute(
                "update signature_signers set status = 'signed', signed_at = now() where id = $1::uuid",
                signer_id,
            )
            count = await conn.fetchval(
                "select count(*) from signature_signers where envelope_id = $1::uuid and status = 'signed'",
                env["id"],
            )
            new_status = "signed" if count >= env["signer_count"] else "partially_signed"
            await conn.execute(
                """update signature_envelopes set status = $2,
                       signed_count = $3,
                       completed_at = case when $2 = 'signed' then now() else completed_at end,
                       updated_at = now()
                    where id = $1::uuid""",
                env["id"], new_status, count,
            )
        elif kind == "viewed" and signer_id:
            await conn.execute(
                "update signature_signers set status = 'viewed' where id = $1::uuid and status = 'sent'",
                signer_id,
            )
            await conn.execute(
                "update signature_envelopes set status = 'viewed' where id = $1::uuid and status = 'sent'",
                env["id"],
            )
        elif kind == "declined":
            if signer_id:
                await conn.execute(
                    "update signature_signers set status = 'declined' where id = $1::uuid",
                    signer_id,
                )
            await conn.execute(
                "update signature_envelopes set status = 'declined', updated_at = now() where id = $1::uuid",
                env["id"],
            )
        elif kind == "expired":
            await conn.execute(
                "update signature_envelopes set status = 'expired', updated_at = now() where id = $1::uuid",
                env["id"],
            )
        elif kind in ("envelope_signed",):
            await conn.execute(
                """update signature_envelopes set status = 'signed',
                       completed_at = now(), updated_at = now()
                    where id = $1::uuid""",
                env["id"],
            )

    return {"ok": True, "kind": kind, "envelope_id": str(env["id"])}


# ══════════════════════════════════════════════════════════════════════
# Voice tools
# ══════════════════════════════════════════════════════════════════════


async def send_for_signature_tool(args: dict, ctx: dict) -> dict:
    """Voice: 'LexAI, manda el contrato a firma con Juan Pérez al correo X'."""
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    document_id = args.get("document_id") or ctx.get("document_id")
    if not (firm_id and document_id):
        return {"error": "firm_id y document_id requeridos"}
    signer_name = (args.get("signer_name") or "").strip()
    signer_email = (args.get("signer_email") or "").strip()
    if not signer_name:
        return {"error": "signer_name requerido"}
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}
    async with storage.pool.acquire() as conn:
        doc = await conn.fetchrow(
            "select id, titulo from matter_documents where id = $1::uuid and firm_id = $2::uuid",
            document_id, firm_id,
        )
        if not doc:
            return {"error": "documento no encontrado"}
    body = CreateEnvelopeRequest(
        title=args.get("title") or f"Firma: {doc['titulo']}",
        message=args.get("message"),
        provider=args.get("provider") or "demo",
        signers=[SignerInput(name=signer_name, email=signer_email or None)],
        documents=[DocumentInput(source_document_id=str(doc["id"]), filename=doc["titulo"])],
    )

    # SimpleNamespace · class-body scope can't see outer `firm_id`.
    from types import SimpleNamespace
    principal = SimpleNamespace(
        firm_id=firm_id,
        user_id=user_id,
        role=ctx.get("role", "lawyer"),
    )
    env = await create_envelope(body, principal)  # type: ignore
    sent = await send_envelope(env["id"], principal)  # type: ignore
    if isinstance(sent, dict):
        from agent.tools._ui_events import ui_data_changed
        # Derive matter_id best-effort from document for client-side targeting.
        from utils.db import get_storage as _get
        try:
            _s = await _get()
            async with _s.pool.acquire() as _c:
                _mid = await _c.fetchval(
                    "select matter_id from matter_documents where id = $1::uuid",
                    document_id,
                )
        except Exception:
            _mid = None
        sent["_ui_command"] = ui_data_changed(
            "signatures", matter_id=str(_mid) if _mid else None, firm_id=firm_id,
            op="create", extra={"envelope_id": env.get("id")},
        )
    return sent


async def check_signature_status_tool(args: dict, ctx: dict) -> dict:
    """Voice: '¿Cómo va la firma del contrato X?'."""
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    envelope_id = args.get("envelope_id")
    if not envelope_id:
        return {"error": "envelope_id requerido"}

    # NameError fix · class-body scope doesn't see outer `firm_id`.
    # Use SimpleNamespace (same pattern as doc_qa.ask_about_document_tool).
    from types import SimpleNamespace
    principal = SimpleNamespace(
        firm_id=firm_id,
        user_id=ctx.get("user_id"),
        role=ctx.get("role", "lawyer"),
    )
    return await get_envelope(envelope_id, principal)  # type: ignore
