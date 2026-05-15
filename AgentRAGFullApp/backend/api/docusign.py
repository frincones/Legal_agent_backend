"""Sprint D · Router /v1/docusign · firma electrónica desde Canvas.

Endpoints:
  POST   /v1/docusign/envelopes              · crear + enviar (con doc inline o ref)
  GET    /v1/docusign/envelopes              · lista por firm/matter
  GET    /v1/docusign/envelopes/{id}         · estado actual (refresh desde DocuSign opcional)
  POST   /v1/docusign/envelopes/{id}/resend
  POST   /v1/docusign/envelopes/{id}/void

Auth: get_current_firm (JWT Supabase).
Reusa api/signatures.py webhook receiver para callbacks de DocuSign
(/v1/signatures/webhook/docusign · sprint 13).
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm
from utils import crypto
from utils.oauth import refresh_access_token
from utils.docusign_client import DocuSignClient

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/docusign", tags=["docusign"])


class Signer(BaseModel):
    name: str = Field(..., min_length=1)
    email: str = Field(..., min_length=3)
    routing_order: int = 1


class CreateEnvelopeBody(BaseModel):
    matter_id: UUID
    title: str = Field(..., min_length=1, max_length=200)
    message: Optional[str] = ""
    signers: list[Signer] = Field(..., min_length=1, max_length=10)
    # Documento: o bien content_base64 inline, o source_document_id
    source_document_id: Optional[UUID] = None
    document_name: Optional[str] = None
    content_base64: Optional[str] = None  # data URL o b64 puro
    email_subject: Optional[str] = None


async def _get_integration(pool, firm_id: str, user_id: str):
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            select id, oauth_access_token_enc, oauth_refresh_token_enc,
                   oauth_expires_at, metadata, account_id
              from firm_integrations
             where firm_id = $1::uuid and user_id = $2::uuid
               and provider = 'docusign' and active = true and status = 'connected'
             limit 1
            """,
            firm_id, user_id,
        )


async def _get_access_token(pool, integration) -> Optional[str]:
    token = crypto.decrypt(integration["oauth_access_token_enc"])
    if not token:
        return None
    expires_at = integration.get("oauth_expires_at")
    now = datetime.now(timezone.utc)
    if expires_at and expires_at < now + timedelta(minutes=5):
        refresh = crypto.decrypt(integration["oauth_refresh_token_enc"])
        if not refresh:
            return token
        new_tokens = await refresh_access_token("docusign", refresh_token=refresh)
        if new_tokens and new_tokens.get("access_token"):
            new_token = new_tokens["access_token"]
            new_enc = crypto.encrypt(new_token)
            new_exp = now + timedelta(seconds=int(new_tokens.get("expires_in") or 3600))
            async with pool.acquire() as conn:
                await conn.execute(
                    "update firm_integrations set oauth_access_token_enc=$1, oauth_expires_at=$2, updated_at=now() where id=$3",
                    new_enc, new_exp, integration["id"],
                )
            return new_token
    return token


async def _resolve_document(pool, body: CreateEnvelopeBody, firm_id: str) -> tuple[bytes, str]:
    """Retorna (bytes, name). De inline base64 o de Storage referenciado."""
    if body.content_base64:
        raw = body.content_base64
        if raw.startswith("data:"):
            raw = raw.split(",", 1)[-1]
        try:
            return base64.b64decode(raw), (body.document_name or "documento.pdf")
        except Exception:
            raise HTTPException(400, "content_base64 inválido")

    if body.source_document_id:
        async with pool.acquire() as conn:
            doc = await conn.fetchrow(
                "select storage_path, titulo, mime_type from matter_documents where id = $1::uuid and firm_id = $2::uuid",
                body.source_document_id, firm_id,
            )
        if not doc or not doc["storage_path"]:
            raise HTTPException(404, "Source document not found")
        # Descargar desde Supabase Storage
        import os
        supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
        service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        url = f"{supabase_url}/storage/v1/object/documents/{doc['storage_path']}"
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.get(url, headers={"Authorization": f"Bearer {service_key}"})
            if r.status_code != 200:
                raise HTTPException(500, "Failed to fetch source document from Storage")
            return r.content, doc["titulo"]

    raise HTTPException(400, "Provide source_document_id or content_base64")


@router.post("/envelopes")
async def create_envelope(
    body: CreateEnvelopeBody,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()

    integration = await _get_integration(storage.pool, principal.firm_id, principal.user_id)
    if not integration:
        raise HTTPException(
            409,
            detail={"error": "docusign_not_connected",
                    "message": "Conecta DocuSign en /settings/integraciones primero."},
        )
    token = await _get_access_token(storage.pool, integration)
    if not token:
        raise HTTPException(500, "Failed to obtain DocuSign access token")

    # Resolver documento
    doc_bytes, doc_name = await _resolve_document(storage.pool, body, principal.firm_id)

    # Get account_id (DocuSign requires it for API paths)
    account_id = integration.get("account_id")
    if not account_id:
        # Hacer userinfo lookup para obtenerlo
        ds_client_tmp = DocuSignClient(token, "TEMP")
        account_id = await ds_client_tmp.get_user_account_id()
        if not account_id:
            raise HTTPException(500, "Failed to resolve DocuSign account_id")
        # Persistir para futuras llamadas
        async with storage.pool.acquire() as conn:
            await conn.execute(
                "update firm_integrations set account_id = $1, updated_at = now() where id = $2",
                account_id, integration["id"],
            )

    ds = DocuSignClient(token, account_id)
    signers_payload = [
        {"name": s.name, "email": s.email, "routing_order": s.routing_order, "recipient_id": str(i + 1)}
        for i, s in enumerate(body.signers)
    ]
    result = await ds.create_envelope(
        title=body.title,
        message=body.message or "",
        document_name=doc_name,
        document_bytes=doc_bytes,
        signers=signers_payload,
        email_subject=body.email_subject or body.title,
    )
    if not result:
        raise HTTPException(502, "DocuSign envelope creation failed")

    envelope_id = result.get("envelopeId")
    status = result.get("status", "sent")

    # Persist en signature_envelopes
    import secrets
    webhook_secret = secrets.token_urlsafe(32)
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into signature_envelopes
              (firm_id, matter_id, title, message, provider, external_id,
               status, signer_count, signed_count, signers, signer_order,
               webhook_secret, source_document_id, created_by, sent_at)
            values ($1::uuid, $2::uuid, $3, $4, 'docusign', $5,
                    $6, $7, 0, $8::jsonb, 'sequential',
                    $9, $10, $11::uuid, now())
            returning id
            """,
            principal.firm_id, body.matter_id, body.title, body.message,
            envelope_id, status, len(body.signers),
            json.dumps([s.model_dump() for s in body.signers]),
            webhook_secret, body.source_document_id, principal.user_id,
        )

    return {
        "ok": True,
        "id": str(row["id"]),
        "envelope_id": envelope_id,
        "status": status,
        "title": body.title,
        "signer_count": len(body.signers),
    }


@router.get("/envelopes")
async def list_envelopes(
    matter_id: Optional[UUID] = Query(None),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        if matter_id:
            rows = await conn.fetch(
                """
                select id, matter_id, title, provider, external_id, status,
                       signer_count, signed_count, signers, sent_at,
                       completed_at, expires_at, signed_pdf_storage_path,
                       created_at
                  from signature_envelopes
                 where firm_id = $1::uuid and matter_id = $2::uuid
                 order by created_at desc limit 100
                """,
                principal.firm_id, matter_id,
            )
        else:
            rows = await conn.fetch(
                """
                select id, matter_id, title, provider, external_id, status,
                       signer_count, signed_count, signers, sent_at,
                       completed_at, expires_at, signed_pdf_storage_path,
                       created_at
                  from signature_envelopes
                 where firm_id = $1::uuid
                 order by created_at desc limit 100
                """,
                principal.firm_id,
            )
    return [
        {
            "id": str(r["id"]),
            "matter_id": str(r["matter_id"]) if r["matter_id"] else None,
            "title": r["title"],
            "provider": r["provider"],
            "external_id": r["external_id"],
            "status": r["status"],
            "signer_count": r["signer_count"],
            "signed_count": r["signed_count"],
            "signers": json.loads(r["signers"]) if isinstance(r["signers"], str) else (r["signers"] or []),
            "sent_at": r["sent_at"].isoformat() if r["sent_at"] else None,
            "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
            "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
            "signed_pdf_storage_path": r["signed_pdf_storage_path"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


@router.get("/envelopes/{envelope_id}")
async def get_envelope(
    envelope_id: UUID,
    refresh: bool = Query(False, description="Si true, consulta DocuSign para refrescar estado"),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, matter_id, title, message, provider, external_id, status,
                   signer_count, signed_count, signers, signed_pdf_storage_path,
                   sent_at, completed_at, expires_at, created_at
              from signature_envelopes
             where id = $1::uuid and firm_id = $2::uuid
            """,
            envelope_id, principal.firm_id,
        )
    if not row:
        raise HTTPException(404, "envelope not found")

    if refresh and row["provider"] == "docusign" and row["external_id"]:
        # Refrescar desde DocuSign
        integration = await _get_integration(storage.pool, principal.firm_id, principal.user_id)
        if integration:
            token = await _get_access_token(storage.pool, integration)
            account_id = integration.get("account_id")
            if token and account_id:
                ds = DocuSignClient(token, account_id)
                ext = await ds.get_envelope(row["external_id"])
                if ext:
                    new_status = ext.get("status", row["status"])
                    async with storage.pool.acquire() as conn:
                        await conn.execute(
                            "update signature_envelopes set status = $1, updated_at = now() where id = $2",
                            new_status, row["id"],
                        )
                    row = dict(row)
                    row["status"] = new_status

    d = dict(row)
    d["id"] = str(d["id"])
    if d.get("matter_id"):
        d["matter_id"] = str(d["matter_id"])
    if isinstance(d.get("signers"), str):
        d["signers"] = json.loads(d["signers"])
    for k in ("sent_at", "completed_at", "expires_at", "created_at"):
        if d.get(k):
            d[k] = d[k].isoformat()
    return d


@router.post("/envelopes/{envelope_id}/resend")
async def resend_envelope(
    envelope_id: UUID,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            "select external_id from signature_envelopes where id = $1::uuid and firm_id = $2::uuid and provider = 'docusign'",
            envelope_id, principal.firm_id,
        )
    if not row:
        raise HTTPException(404, "envelope not found")
    integration = await _get_integration(storage.pool, principal.firm_id, principal.user_id)
    if not integration:
        raise HTTPException(409, "DocuSign not connected")
    token = await _get_access_token(storage.pool, integration)
    account_id = integration.get("account_id")
    if not token or not account_id:
        raise HTTPException(500, "Failed to obtain DocuSign credentials")
    ok = await DocuSignClient(token, account_id).resend(row["external_id"])
    return {"ok": ok}


@router.post("/envelopes/{envelope_id}/void")
async def void_envelope(
    envelope_id: UUID,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            "select external_id from signature_envelopes where id = $1::uuid and firm_id = $2::uuid and provider = 'docusign'",
            envelope_id, principal.firm_id,
        )
    if not row:
        raise HTTPException(404, "envelope not found")
    integration = await _get_integration(storage.pool, principal.firm_id, principal.user_id)
    if not integration:
        raise HTTPException(409, "DocuSign not connected")
    token = await _get_access_token(storage.pool, integration)
    account_id = integration.get("account_id")
    if not token or not account_id:
        raise HTTPException(500, "Failed to obtain DocuSign credentials")
    ok = await DocuSignClient(token, account_id).void_envelope(row["external_id"])
    if ok:
        async with storage.pool.acquire() as conn:
            await conn.execute(
                "update signature_envelopes set status = 'voided', canceled_at = now(), updated_at = now() where id = $1",
                envelope_id,
            )
    return {"ok": ok}
