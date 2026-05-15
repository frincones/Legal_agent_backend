"""Sprint C · Worker · sync de carpetas vigiladas (Drive/OneDrive/Dropbox).

Patrón:
  1. Iterar cloud_folder_watchers activos
  2. Por cada watcher:
     a. Descifrar access_token del integration
     b. Refresh si expirado
     c. list_changes(cursor) según provider
     d. Para cada item:
        - Si es archivo nuevo/modificado: download + subir a Supabase Storage
          + upsert matter_documents (idempotente por source_external_id)
        - Si fue eliminado: marcar status='archived' en matter_documents
     e. Update watcher.last_sync_cursor + last_synced_at
  3. Reporta stats

Invocado por:
  - pg_cron 'cloud_delta_sync' cada 30min (vía /v1/admin/sync-tick?type=cloud)
  - Manualmente desde POST /v1/cloud/watchers/{id}/sync-now
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from utils import crypto
from utils.oauth import refresh_access_token
from utils.cloud_clients import (
    GoogleDriveClient,
    MicrosoftGraphDriveClient,
    DropboxClient,
)

logger = logging.getLogger(__name__)


ALLOWED_MIME_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "text/plain",
    "text/csv",
    "image/png",
    "image/jpeg",
    "image/webp",
    # Google Docs · se exportan a PDF
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
}

MAX_FILE_BYTES = 25 * 1024 * 1024  # 25 MB hard limit


async def _get_valid_access_token(pool, integration_row) -> Optional[str]:
    """Descifra access_token. Refresca si está por expirar."""
    access = crypto.decrypt(integration_row["oauth_access_token_enc"])
    if not access:
        return None
    expires_at = integration_row.get("oauth_expires_at")
    now = datetime.now(timezone.utc)
    if expires_at and expires_at < now + timedelta(minutes=5):
        refresh = crypto.decrypt(integration_row.get("oauth_refresh_token_enc"))
        if not refresh:
            return access  # try with stale token
        new_tokens = await refresh_access_token(
            integration_row["provider"], refresh_token=refresh
        )
        if new_tokens and new_tokens.get("access_token"):
            new_access = new_tokens["access_token"]
            new_access_enc = crypto.encrypt(new_access)
            new_expires = now + timedelta(seconds=int(new_tokens.get("expires_in") or 3600))
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    update firm_integrations
                       set oauth_access_token_enc = $1, oauth_expires_at = $2,
                           updated_at = now()
                     where id = $3
                    """,
                    new_access_enc, new_expires, integration_row["id"],
                )
            return new_access
    return access


def _client_for(provider: str, access_token: str):
    if provider == "google_drive":
        return GoogleDriveClient(access_token)
    if provider == "onedrive":
        return MicrosoftGraphDriveClient(access_token)
    if provider == "dropbox":
        return DropboxClient(access_token)
    raise ValueError(f"Unsupported cloud provider: {provider}")


def _extract_metadata(provider: str, item: dict) -> dict:
    """Normaliza item entre providers."""
    if provider == "google_drive":
        # Item es 'change' dict si viene de changes API
        file = item.get("file") if "file" in item else item
        if not file:
            return {"removed": True, "external_id": item.get("fileId")}
        return {
            "external_id": file.get("id"),
            "name": file.get("name", "untitled"),
            "mime_type": file.get("mimeType"),
            "size": int(file.get("size") or 0),
            "url": file.get("webViewLink"),
            "modified": file.get("modifiedTime"),
            "checksum": file.get("md5Checksum"),
            "removed": item.get("removed", False),
            "parents": file.get("parents", []),
        }
    if provider == "onedrive":
        tag = item.get("deleted")
        if tag:
            return {"removed": True, "external_id": item.get("id")}
        f = item.get("file") or {}
        return {
            "external_id": item.get("id"),
            "name": item.get("name", "untitled"),
            "mime_type": f.get("mimeType"),
            "size": int(item.get("size") or 0),
            "url": item.get("webUrl"),
            "modified": item.get("lastModifiedDateTime"),
            "removed": False,
            "parent_id": (item.get("parentReference") or {}).get("id"),
        }
    if provider == "dropbox":
        tag = item.get(".tag")
        if tag == "deleted":
            return {"removed": True, "external_id": item.get("path_lower")}
        if tag != "file":
            return {"skip": True}
        return {
            "external_id": item.get("id"),
            "name": item.get("name", "untitled"),
            "mime_type": None,  # Dropbox no provee mime · se infiere por ext
            "size": int(item.get("size") or 0),
            "url": None,
            "modified": item.get("client_modified") or item.get("server_modified"),
            "removed": False,
            "path": item.get("path_lower"),
        }
    return {}


async def _is_in_watched_folder(item_meta: dict, watcher: dict) -> bool:
    """Verifica que el archivo esté dentro de la carpeta vigilada."""
    folder_id = watcher["cloud_folder_id"]
    if "parents" in item_meta and item_meta["parents"]:
        return folder_id in item_meta["parents"]
    if "parent_id" in item_meta:
        return item_meta["parent_id"] == folder_id
    if "path" in item_meta and item_meta["path"]:
        return item_meta["path"].startswith(folder_id.lower())
    return False


async def sync_watcher(watcher_id: str) -> dict[str, Any]:
    """Sincroniza una carpeta vigilada."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "no storage"}

    async with storage.pool.acquire() as conn:
        watcher = await conn.fetchrow(
            """
            select w.id, w.firm_id, w.integration_id, w.provider,
                   w.cloud_folder_id, w.folder_path, w.matter_id,
                   w.last_sync_cursor,
                   fi.oauth_access_token_enc, fi.oauth_refresh_token_enc,
                   fi.oauth_expires_at
              from cloud_folder_watchers w
              join firm_integrations fi on fi.id = w.integration_id
             where w.id = $1::uuid and w.active = true
            """,
            watcher_id,
        )
    if not watcher:
        return {"error": "watcher not found or inactive"}

    integration_row = {
        "id": watcher["integration_id"],
        "provider": watcher["provider"],
        "oauth_access_token_enc": watcher["oauth_access_token_enc"],
        "oauth_refresh_token_enc": watcher["oauth_refresh_token_enc"],
        "oauth_expires_at": watcher["oauth_expires_at"],
    }
    access_token = await _get_valid_access_token(storage.pool, integration_row)
    if not access_token:
        return {"error": "access_token decrypt failed"}

    provider = watcher["provider"]
    client = _client_for(provider, access_token)

    # Llamar list_changes según provider
    try:
        if provider == "dropbox":
            changes = await client.list_changes(
                cursor=watcher["last_sync_cursor"],
                path=watcher["folder_path"] or "",
            )
        else:
            changes = await client.list_changes(cursor=watcher["last_sync_cursor"])
    except Exception as e:
        logger.warning("list_changes failed watcher=%s: %s", watcher_id, e)
        return {"error": f"list_changes: {str(e)[:120]}"}

    if changes.get("expired"):
        # Cursor expirado · reset y siguiente run hace full scan
        async with storage.pool.acquire() as conn:
            await conn.execute(
                "update cloud_folder_watchers set last_sync_cursor = null, last_synced_at = now() where id = $1",
                watcher["id"],
            )
        return {"expired": True}

    inserted = 0
    updated = 0
    removed = 0
    skipped = 0
    errors: list[str] = []

    for raw in changes.get("items", []):
        meta = _extract_metadata(provider, raw)
        if meta.get("skip"):
            skipped += 1
            continue
        external_id = meta.get("external_id")
        if not external_id:
            skipped += 1
            continue

        if meta.get("removed"):
            async with storage.pool.acquire() as conn:
                r = await conn.fetchrow(
                    """
                    update matter_documents
                       set status = 'archived', updated_at = now()
                     where source_integration_id = $1::uuid
                       and source_external_id = $2
                     returning id
                    """,
                    watcher["integration_id"], external_id,
                )
                if r:
                    removed += 1
            continue

        # Solo procesar archivos dentro de la carpeta vigilada
        if not await _is_in_watched_folder(meta, watcher):
            skipped += 1
            continue

        # Filtrar por mime_type permitido (cuando aplica)
        mime = meta.get("mime_type")
        if provider != "dropbox" and mime and mime not in ALLOWED_MIME_TYPES:
            skipped += 1
            continue

        # Límite de tamaño
        if meta.get("size", 0) > MAX_FILE_BYTES:
            skipped += 1
            continue

        # Descargar archivo
        try:
            if provider == "google_drive" and mime and mime.startswith("application/vnd.google-apps"):
                content = await client.export_google_doc(external_id)
                effective_mime = "application/pdf"
                effective_name = meta["name"] + ".pdf"
            elif provider == "dropbox":
                content = await client.download(meta["path"])
                effective_mime = None
                effective_name = meta["name"]
            else:
                content = await client.download(external_id)
                effective_mime = mime
                effective_name = meta["name"]
        except Exception as e:
            errors.append(f"download {external_id}: {str(e)[:80]}")
            continue

        if not content:
            errors.append(f"download {external_id}: empty")
            continue

        # Subir a Supabase Storage
        storage_path = f"{watcher['firm_id']}/{watcher['matter_id'] or 'unassigned'}/{external_id}_{effective_name}"
        try:
            await _upload_to_storage(storage_path, content, effective_mime)
        except Exception as e:
            errors.append(f"upload {external_id}: {str(e)[:80]}")
            continue

        # Upsert en matter_documents (idempotente · unique constraint)
        sha = hashlib.sha256(content).hexdigest()
        async with storage.pool.acquire() as conn:
            existing = await conn.fetchrow(
                """
                select id from matter_documents
                 where source_integration_id = $1::uuid
                   and source_external_id = $2
                 limit 1
                """,
                watcher["integration_id"], external_id,
            )
            if existing:
                await conn.execute(
                    """
                    update matter_documents
                       set titulo = $1, storage_path = $2, mime_type = $3,
                           byte_size = $4, sha256 = $5,
                           source_url = $6, last_synced_at = now(),
                           status = 'ready', updated_at = now()
                     where id = $7
                    """,
                    effective_name, storage_path, effective_mime,
                    len(content), sha, meta.get("url"),
                    existing["id"],
                )
                updated += 1
            else:
                if not watcher["matter_id"]:
                    skipped += 1
                    continue
                await conn.execute(
                    """
                    insert into matter_documents
                      (firm_id, matter_id, kind, titulo, status,
                       storage_path, mime_type, byte_size, sha256,
                       source_provider, source_external_id,
                       source_integration_id, source_watcher_id,
                       source_url, last_synced_at, metadata)
                    values ($1::uuid, $2::uuid, 'evidence', $3, 'ready',
                            $4, $5, $6, $7,
                            $8, $9, $10::uuid, $11::uuid,
                            $12, now(), '{}'::jsonb)
                    """,
                    watcher["firm_id"], watcher["matter_id"],
                    effective_name, storage_path, effective_mime,
                    len(content), sha,
                    provider, external_id,
                    watcher["integration_id"], watcher["id"],
                    meta.get("url"),
                )
                inserted += 1

    # Update cursor
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            update cloud_folder_watchers
               set last_sync_cursor = $1, last_synced_at = now(), updated_at = now()
             where id = $2
            """,
            changes.get("next_cursor"), watcher["id"],
        )

    return {
        "watcher_id": str(watcher["id"]),
        "provider": provider,
        "inserted": inserted, "updated": updated,
        "removed": removed, "skipped": skipped,
        "errors": errors[:5], "error_count": len(errors),
    }


async def _upload_to_storage(path: str, content: bytes, mime_type: Optional[str]):
    """Sube bytes a Supabase Storage bucket 'documents'."""
    import os
    import httpx
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not supabase_url or not service_key:
        raise RuntimeError("Supabase storage credentials missing")
    url = f"{supabase_url}/storage/v1/object/documents/{path}"
    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.post(
            url,
            content=content,
            headers={
                "Authorization": f"Bearer {service_key}",
                "Content-Type": mime_type or "application/octet-stream",
                "x-upsert": "true",
            },
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Storage upload failed: {r.status_code} {r.text[:120]}")


async def sync_all_watchers() -> dict[str, Any]:
    """Itera todos los watchers activos. Invocado por pg_cron cada 30min."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "no storage"}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id from cloud_folder_watchers
             where active = true
             order by coalesce(last_synced_at, '1970-01-01'::timestamptz) asc
             limit 100
            """,
        )
    total = {"synced": 0, "inserted": 0, "updated": 0, "removed": 0, "errors": []}
    for r in rows:
        try:
            result = await sync_watcher(str(r["id"]))
            total["synced"] += 1
            total["inserted"] += result.get("inserted", 0)
            total["updated"] += result.get("updated", 0)
            total["removed"] += result.get("removed", 0)
            if result.get("error_count", 0) > 0:
                total["errors"].append(f"{r['id']}: {result.get('errors', [])}")
        except Exception as e:
            logger.warning("sync_all_watchers item %s failed: %s", r["id"], e)
            total["errors"].append(f"{r['id']}: {str(e)[:120]}")
    return total
