"""Sprint C · Clientes unificados de almacenamiento en la nube.

Provee 3 clientes con API similar:
  - GoogleDriveClient (Drive v3)
  - MicrosoftGraphDriveClient (Graph /me/drive)
  - DropboxClient (Dropbox API v2)

Cada uno expone:
  - list_folders()       · árbol de carpetas para el picker
  - list_files(folder)   · archivos directos de una carpeta
  - list_changes(cursor) · delta sync · retorna (items, next_cursor, expired)
  - download(file_id)    · bytes del archivo
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Google Drive
# ─────────────────────────────────────────────────────────────────────

class GoogleDriveClient:
    BASE = "https://www.googleapis.com/drive/v3"

    def __init__(self, access_token: str):
        self.access_token = access_token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token}"}

    async def list_folders(self, *, parent_id: Optional[str] = None, page_size: int = 100) -> list[dict]:
        """Lista carpetas (sin recursión · pickear nivel por nivel)."""
        q_parts = ["mimeType = 'application/vnd.google-apps.folder'", "trashed = false"]
        if parent_id:
            q_parts.append(f"'{parent_id}' in parents")
        params = {
            "q": " and ".join(q_parts),
            "pageSize": str(page_size),
            "fields": "files(id,name,modifiedTime,parents,webViewLink)",
        }
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(f"{self.BASE}/files", params=params, headers=self._headers())
            r.raise_for_status()
            return r.json().get("files", [])

    async def list_files(self, folder_id: str, *, page_size: int = 200) -> list[dict]:
        params = {
            "q": f"'{folder_id}' in parents and trashed = false and mimeType != 'application/vnd.google-apps.folder'",
            "pageSize": str(page_size),
            "fields": "files(id,name,mimeType,size,modifiedTime,webViewLink,md5Checksum)",
        }
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(f"{self.BASE}/files", params=params, headers=self._headers())
            r.raise_for_status()
            return r.json().get("files", [])

    async def get_start_page_token(self) -> Optional[str]:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{self.BASE}/changes/startPageToken", headers=self._headers())
            r.raise_for_status()
            return r.json().get("startPageToken")

    async def list_changes(self, *, cursor: Optional[str]) -> dict[str, Any]:
        """Cambios desde el cursor. Si no hay cursor, retorna current startPageToken sin items."""
        if not cursor:
            new_token = await self.get_start_page_token()
            return {"items": [], "next_cursor": new_token, "expired": False}
        params = {
            "pageToken": cursor,
            "pageSize": "100",
            "fields": "changes(fileId,removed,file(id,name,mimeType,size,parents,modifiedTime,webViewLink,md5Checksum)),newStartPageToken,nextPageToken",
            "includeRemoved": "true",
        }
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.get(f"{self.BASE}/changes", params=params, headers=self._headers())
            if r.status_code == 404 or r.status_code == 410:
                return {"items": [], "next_cursor": None, "expired": True}
            r.raise_for_status()
            data = r.json()
            return {
                "items": data.get("changes", []),
                "next_cursor": data.get("newStartPageToken") or data.get("nextPageToken"),
                "expired": False,
            }

    async def download(self, file_id: str) -> Optional[bytes]:
        params = {"alt": "media"}
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.get(
                f"{self.BASE}/files/{file_id}",
                params=params,
                headers=self._headers(),
            )
            if r.status_code != 200:
                logger.warning("drive download failed: %s", r.status_code)
                return None
            return r.content

    async def export_google_doc(self, file_id: str, mime_export: str = "application/pdf") -> Optional[bytes]:
        """Google Docs/Sheets/Slides necesitan export en lugar de download."""
        params = {"mimeType": mime_export}
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.get(
                f"{self.BASE}/files/{file_id}/export",
                params=params,
                headers=self._headers(),
            )
            if r.status_code != 200:
                return None
            return r.content


# ─────────────────────────────────────────────────────────────────────
# Microsoft Graph (OneDrive)
# ─────────────────────────────────────────────────────────────────────

class MicrosoftGraphDriveClient:
    BASE = "https://graph.microsoft.com/v1.0"

    def __init__(self, access_token: str):
        self.access_token = access_token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token}"}

    async def list_folders(self, *, parent_id: Optional[str] = None) -> list[dict]:
        path = "/me/drive/root/children" if not parent_id else f"/me/drive/items/{parent_id}/children"
        params = {"$top": "100", "$filter": "folder ne null", "$select": "id,name,folder,lastModifiedDateTime,webUrl"}
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(f"{self.BASE}{path}", params=params, headers=self._headers())
            r.raise_for_status()
            return r.json().get("value", [])

    async def list_files(self, folder_id: str) -> list[dict]:
        path = f"/me/drive/items/{folder_id}/children"
        params = {"$top": "200", "$filter": "file ne null", "$select": "id,name,size,file,lastModifiedDateTime,webUrl"}
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(f"{self.BASE}{path}", params=params, headers=self._headers())
            r.raise_for_status()
            return r.json().get("value", [])

    async def list_changes(self, *, cursor: Optional[str]) -> dict[str, Any]:
        """Delta query. cursor = deltaLink completo o None."""
        if cursor:
            url = cursor
            params = None
        else:
            url = f"{self.BASE}/me/drive/root/delta"
            params = {"$select": "id,name,size,file,folder,parentReference,lastModifiedDateTime,webUrl,deleted"}
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.get(url, params=params, headers=self._headers())
            r.raise_for_status()
            data = r.json()
            return {
                "items": data.get("value", []),
                "next_cursor": data.get("@odata.deltaLink") or data.get("@odata.nextLink"),
                "expired": False,
            }

    async def download(self, file_id: str) -> Optional[bytes]:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as c:
            r = await c.get(f"{self.BASE}/me/drive/items/{file_id}/content", headers=self._headers())
            if r.status_code != 200:
                return None
            return r.content


# ─────────────────────────────────────────────────────────────────────
# Dropbox
# ─────────────────────────────────────────────────────────────────────

class DropboxClient:
    BASE = "https://api.dropboxapi.com/2"
    CONTENT = "https://content.dropboxapi.com/2"

    def __init__(self, access_token: str):
        self.access_token = access_token

    def _headers(self, json_arg: Optional[dict] = None) -> dict:
        h = {"Authorization": f"Bearer {self.access_token}"}
        if json_arg is not None:
            import json
            h["Dropbox-API-Arg"] = json.dumps(json_arg)
        return h

    async def list_folders(self, *, path: str = "") -> list[dict]:
        """List entries in a folder, filtered to folders only."""
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(
                f"{self.BASE}/files/list_folder",
                headers={**self._headers(), "Content-Type": "application/json"},
                json={"path": path, "recursive": False, "limit": 200},
            )
            r.raise_for_status()
            entries = r.json().get("entries", [])
            return [e for e in entries if e.get(".tag") == "folder"]

    async def list_files(self, path: str = "") -> list[dict]:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(
                f"{self.BASE}/files/list_folder",
                headers={**self._headers(), "Content-Type": "application/json"},
                json={"path": path, "recursive": False, "limit": 500},
            )
            r.raise_for_status()
            entries = r.json().get("entries", [])
            return [e for e in entries if e.get(".tag") == "file"]

    async def list_changes(self, *, cursor: Optional[str], path: str = "") -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as c:
            if cursor:
                r = await c.post(
                    f"{self.BASE}/files/list_folder/continue",
                    headers={**self._headers(), "Content-Type": "application/json"},
                    json={"cursor": cursor},
                )
            else:
                # Get latest cursor without items
                r = await c.post(
                    f"{self.BASE}/files/list_folder/get_latest_cursor",
                    headers={**self._headers(), "Content-Type": "application/json"},
                    json={"path": path, "recursive": True},
                )
                if r.status_code != 200:
                    return {"items": [], "next_cursor": None, "expired": True}
                return {"items": [], "next_cursor": r.json().get("cursor"), "expired": False}

            if r.status_code != 200:
                return {"items": [], "next_cursor": None, "expired": True}
            data = r.json()
            return {
                "items": data.get("entries", []),
                "next_cursor": data.get("cursor"),
                "expired": False,
            }

    async def download(self, path: str) -> Optional[bytes]:
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.post(
                f"{self.CONTENT}/files/download",
                headers=self._headers({"path": path}),
            )
            if r.status_code != 200:
                return None
            return r.content
