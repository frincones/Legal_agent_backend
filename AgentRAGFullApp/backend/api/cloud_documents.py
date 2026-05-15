"""Sprint C · Router /v1/cloud · gestión de carpetas vigiladas + picker.

Endpoints (válidos para los 3 providers: google_drive | onedrive | dropbox):
  GET    /v1/cloud/{provider}/folders            · árbol de carpetas
  GET    /v1/cloud/{provider}/folders/{id}/files · archivos de una carpeta
  GET    /v1/cloud/watchers                      · watchers de la firma
  POST   /v1/cloud/watchers                      · crear watcher (folder ↔ matter)
  DELETE /v1/cloud/watchers/{id}                 · borrar watcher (soft)
  POST   /v1/cloud/watchers/{id}/sync-now        · forzar sync inmediato

Auth: get_current_firm (JWT Supabase).
"""

from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm
from utils import crypto
from utils.cloud_clients import (
    GoogleDriveClient,
    MicrosoftGraphDriveClient,
    DropboxClient,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/cloud", tags=["cloud"])

SUPPORTED_PROVIDERS = {"google_drive", "onedrive", "dropbox"}


def _client_for(provider: str, access_token: str):
    if provider == "google_drive":
        return GoogleDriveClient(access_token)
    if provider == "onedrive":
        return MicrosoftGraphDriveClient(access_token)
    if provider == "dropbox":
        return DropboxClient(access_token)
    raise HTTPException(400, f"Unsupported provider: {provider}")


async def _get_connected_integration(pool, firm_id: str, user_id: str, provider: str):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, oauth_access_token_enc
              from firm_integrations
             where firm_id = $1::uuid and user_id = $2::uuid
               and provider = $3 and active = true and status = 'connected'
             limit 1
            """,
            firm_id, user_id, provider,
        )
    return row


async def _decrypt_token_or_400(integration) -> str:
    if not integration:
        raise HTTPException(
            409,
            detail={"error": "not_connected",
                    "message": "Conecta este proveedor en /settings/integraciones primero."},
        )
    token = crypto.decrypt(integration["oauth_access_token_enc"])
    if not token:
        raise HTTPException(500, "Failed to decrypt access token")
    return token


@router.get("/{provider}/folders")
async def list_folders(
    provider: str,
    parent_id: Optional[str] = None,
    principal: Principal = Depends(get_current_firm),
):
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(400, f"Unsupported provider: {provider}")
    from utils.db import get_storage
    storage = await get_storage()
    integration = await _get_connected_integration(
        storage.pool, principal.firm_id, principal.user_id, provider
    )
    token = await _decrypt_token_or_400(integration)
    client = _client_for(provider, token)
    try:
        if provider == "dropbox":
            folders = await client.list_folders(path=parent_id or "")
            return {"folders": [
                {"id": f.get("path_lower"),
                 "name": f.get("name"),
                 "path": f.get("path_display")}
                for f in folders
            ]}
        folders = await client.list_folders(parent_id=parent_id)
        return {"folders": [
            {"id": f.get("id"),
             "name": f.get("name"),
             "modified": f.get("modifiedTime") or f.get("lastModifiedDateTime"),
             "url": f.get("webViewLink") or f.get("webUrl")}
            for f in folders
        ]}
    except Exception as e:
        logger.warning("list_folders %s failed: %s", provider, e)
        raise HTTPException(502, detail={"error": "provider_error", "message": str(e)[:160]})


@router.get("/{provider}/folders/{folder_id}/files")
async def list_files_in_folder(
    provider: str,
    folder_id: str,
    principal: Principal = Depends(get_current_firm),
):
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(400, f"Unsupported provider: {provider}")
    from utils.db import get_storage
    storage = await get_storage()
    integration = await _get_connected_integration(
        storage.pool, principal.firm_id, principal.user_id, provider
    )
    token = await _decrypt_token_or_400(integration)
    client = _client_for(provider, token)
    try:
        if provider == "dropbox":
            files = await client.list_files(path=folder_id)
            return {"files": [
                {"id": f.get("id"), "name": f.get("name"),
                 "size": f.get("size"), "modified": f.get("server_modified"),
                 "path": f.get("path_lower")}
                for f in files
            ]}
        files = await client.list_files(folder_id)
        return {"files": [
            {"id": f.get("id"), "name": f.get("name"),
             "mime_type": f.get("mimeType") or (f.get("file") or {}).get("mimeType"),
             "size": int(f.get("size") or 0),
             "modified": f.get("modifiedTime") or f.get("lastModifiedDateTime"),
             "url": f.get("webViewLink") or f.get("webUrl")}
            for f in files
        ]}
    except Exception as e:
        logger.warning("list_files %s failed: %s", provider, e)
        raise HTTPException(502, detail={"error": "provider_error", "message": str(e)[:160]})


class CreateWatcherBody(BaseModel):
    provider: str = Field(..., pattern="^(google_drive|onedrive|dropbox)$")
    cloud_folder_id: str = Field(..., min_length=1)
    folder_path: Optional[str] = None
    matter_id: Optional[UUID] = None
    auto_match_by_name: bool = True


@router.get("/watchers")
async def list_watchers(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select w.id, w.provider, w.cloud_folder_id, w.folder_path,
                   w.matter_id, m.titulo as matter_titulo,
                   w.last_synced_at, w.created_at,
                   fi.account_label
              from cloud_folder_watchers w
              left join matters m on m.id = w.matter_id
              left join firm_integrations fi on fi.id = w.integration_id
             where w.firm_id = $1::uuid and w.active = true
             order by w.created_at desc
            """,
            principal.firm_id,
        )
    return [
        {
            "id": str(r["id"]),
            "provider": r["provider"],
            "cloud_folder_id": r["cloud_folder_id"],
            "folder_path": r["folder_path"],
            "matter_id": str(r["matter_id"]) if r["matter_id"] else None,
            "matter_titulo": r["matter_titulo"],
            "account_label": r["account_label"],
            "last_synced_at": r["last_synced_at"].isoformat() if r["last_synced_at"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


@router.post("/watchers")
async def create_watcher(
    body: CreateWatcherBody,
    principal: Principal = Depends(get_current_firm),
):
    """Crea watcher · una carpeta vinculada a un caso."""
    from utils.db import get_storage
    storage = await get_storage()

    integration = await _get_connected_integration(
        storage.pool, principal.firm_id, principal.user_id, body.provider
    )
    if not integration:
        raise HTTPException(
            409,
            detail={"error": "not_connected",
                    "message": f"Conecta {body.provider} primero."},
        )

    # Validar matter pertenece a la firma
    if body.matter_id:
        async with storage.pool.acquire() as conn:
            m = await conn.fetchrow(
                "select 1 from matters where id = $1::uuid and firm_id = $2::uuid",
                body.matter_id, principal.firm_id,
            )
            if not m:
                raise HTTPException(404, "matter not found in your firm")

    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into cloud_folder_watchers
              (firm_id, integration_id, provider, cloud_folder_id, folder_path,
               matter_id, auto_match_by_name)
            values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7)
            on conflict (integration_id, cloud_folder_id) do update
              set active = true, matter_id = excluded.matter_id,
                  folder_path = excluded.folder_path,
                  auto_match_by_name = excluded.auto_match_by_name,
                  updated_at = now()
            returning id
            """,
            principal.firm_id, integration["id"], body.provider,
            body.cloud_folder_id, body.folder_path,
            body.matter_id, body.auto_match_by_name,
        )

    # Disparar primer sync en background (fire and forget)
    import asyncio
    from agent.workers.cloud_sync import sync_watcher
    asyncio.create_task(sync_watcher(str(row["id"])))

    return {"ok": True, "id": str(row["id"]), "provider": body.provider}


@router.delete("/watchers/{watcher_id}")
async def delete_watcher(
    watcher_id: UUID,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            update cloud_folder_watchers
               set active = false, updated_at = now()
             where id = $1::uuid and firm_id = $2::uuid
             returning id
            """,
            watcher_id, principal.firm_id,
        )
    if not r:
        raise HTTPException(404, "watcher not found")
    return {"ok": True}


@router.post("/watchers/{watcher_id}/sync-now")
async def sync_now(
    watcher_id: UUID,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        r = await conn.fetchrow(
            "select id from cloud_folder_watchers where id = $1::uuid and firm_id = $2::uuid and active = true",
            watcher_id, principal.firm_id,
        )
    if not r:
        raise HTTPException(404, "watcher not found")
    from agent.workers.cloud_sync import sync_watcher
    result = await sync_watcher(str(watcher_id))
    return result
