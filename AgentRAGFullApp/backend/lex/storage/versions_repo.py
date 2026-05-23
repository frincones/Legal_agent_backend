"""Repo CRUD para document_versions (Sprint M6)."""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


class VersionsRepo:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def create_version(
        self,
        document_id: str,
        change_type: str,
        blocks_snapshot: list[dict[str, Any]],
        section_key: str | None = None,
        parent_version_id: str | None = None,
        feedback: str | None = None,
        created_by: str | None = None,
        blocks_diff: dict | None = None,
    ) -> dict | None:
        """Crea nueva versión. Auto-incrementa version_num."""
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow("""
                        SELECT COALESCE(MAX(version_num), 0) + 1 AS next_v
                        FROM document_versions
                        WHERE document_id = $1
                    """, uuid.UUID(document_id))
                    next_v = row["next_v"]

                    row2 = await conn.fetchrow("""
                        INSERT INTO document_versions
                            (document_id, version_num, parent_version_id,
                             change_type, section_key, blocks_snapshot,
                             blocks_diff, feedback, created_by)
                        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8, $9)
                        RETURNING id, version_num, created_at
                    """,
                        uuid.UUID(document_id), next_v,
                        uuid.UUID(parent_version_id) if parent_version_id else None,
                        change_type, section_key,
                        json.dumps(blocks_snapshot, ensure_ascii=False, default=str),
                        json.dumps(blocks_diff or {}, ensure_ascii=False, default=str),
                        feedback,
                        uuid.UUID(created_by) if created_by else None)
            return {
                "id": str(row2["id"]),
                "version_num": row2["version_num"],
                "created_at": row2["created_at"].isoformat() if row2["created_at"] else None,
            }
        except Exception as e:
            logger.warning("create_version failed: %s", e)
            return None

    async def list_versions(self, document_id: str) -> list[dict]:
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT id, version_num, parent_version_id, change_type,
                           section_key, feedback, created_at
                    FROM document_versions
                    WHERE document_id = $1
                    ORDER BY version_num DESC
                """, uuid.UUID(document_id))
            return [
                {
                    "id": str(r["id"]),
                    "version_num": r["version_num"],
                    "parent_version_id": str(r["parent_version_id"]) if r["parent_version_id"] else None,
                    "change_type": r["change_type"],
                    "section_key": r["section_key"],
                    "feedback": r["feedback"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in rows
            ]
        except Exception as e:
            logger.warning("list_versions failed: %s", e)
            return []

    async def get_version_snapshot(self, document_id: str, version_num: int) -> list[dict] | None:
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT blocks_snapshot FROM document_versions
                    WHERE document_id = $1 AND version_num = $2
                """, uuid.UUID(document_id), version_num)
            if not row:
                return None
            data = row["blocks_snapshot"]
            return data if isinstance(data, list) else json.loads(data or "[]")
        except Exception as e:
            logger.warning("get_version_snapshot failed: %s", e)
            return None

    async def diff_versions(
        self, document_id: str, from_v: int, to_v: int,
    ) -> dict[str, Any]:
        """Diff naive entre dos versiones: lista bloques added/removed/changed."""
        from_blocks = await self.get_version_snapshot(document_id, from_v) or []
        to_blocks = await self.get_version_snapshot(document_id, to_v) or []
        from_ids = {b.get("block_id") for b in from_blocks}
        to_ids = {b.get("block_id") for b in to_blocks}
        added = [b for b in to_blocks if b.get("block_id") not in from_ids]
        removed = [b for b in from_blocks if b.get("block_id") not in to_ids]
        # changed: mismo id pero diferente block_data
        from_map = {b.get("block_id"): b for b in from_blocks}
        to_map = {b.get("block_id"): b for b in to_blocks}
        changed = []
        for bid in from_ids & to_ids:
            if from_map[bid].get("block_data") != to_map[bid].get("block_data"):
                changed.append({
                    "block_id": bid,
                    "from": from_map[bid],
                    "to": to_map[bid],
                })
        return {
            "from_version": from_v,
            "to_version": to_v,
            "added_count": len(added),
            "removed_count": len(removed),
            "changed_count": len(changed),
            "added": added[:50],
            "removed": removed[:50],
            "changed": changed[:50],
        }
