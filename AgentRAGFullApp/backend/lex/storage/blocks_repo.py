"""Repo CRUD para document_blocks."""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


class BlocksRepo:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def insert_block(
        self,
        document_id: str,
        generation_id: str,
        section_key: str,
        block_order: int,
        block_id: str,
        block_type: str,
        block_data: dict[str, Any],
    ) -> None:
        """Inserta un bloque. Idempotente vía ON CONFLICT (block_id)."""
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO document_blocks
                        (document_id, generation_id, section_key, block_order,
                         block_id, block_type, block_data)
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                    ON CONFLICT (block_id) DO UPDATE
                        SET block_data = EXCLUDED.block_data,
                            section_key = EXCLUDED.section_key,
                            block_order = EXCLUDED.block_order
                """, uuid.UUID(document_id), uuid.UUID(generation_id),
                     section_key, block_order, block_id, block_type,
                     json.dumps(block_data, ensure_ascii=False, default=str))
        except Exception as e:
            logger.warning("insert_block failed for %s: %s", block_id, e)

    async def insert_blocks_batch(
        self,
        document_id: str,
        generation_id: str,
        blocks: list[dict[str, Any]],
    ) -> int:
        """Inserta múltiples bloques en una sola transacción."""
        if not blocks:
            return 0
        inserted = 0
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    for b in blocks:
                        await conn.execute("""
                            INSERT INTO document_blocks
                                (document_id, generation_id, section_key, block_order,
                                 block_id, block_type, block_data)
                            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                            ON CONFLICT (block_id) DO NOTHING
                        """, uuid.UUID(document_id), uuid.UUID(generation_id),
                             b["section_key"], b["block_order"], b["block_id"],
                             b["block_type"],
                             json.dumps(b["block_data"], ensure_ascii=False, default=str))
                        inserted += 1
            return inserted
        except Exception as e:
            logger.exception("insert_blocks_batch failed: %s", e)
            return 0

    async def get_blocks_for_document(self, document_id: str) -> list[dict[str, Any]]:
        """Recupera todos los bloques de un documento ordenados."""
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT block_id, section_key, block_order, block_type, block_data, created_at
                    FROM document_blocks
                    WHERE document_id = $1
                    ORDER BY block_order ASC
                """, uuid.UUID(document_id))
            return [
                {
                    "block_id": r["block_id"],
                    "section_key": r["section_key"],
                    "block_order": r["block_order"],
                    "block_type": r["block_type"],
                    "block_data": r["block_data"] if isinstance(r["block_data"], dict)
                                  else json.loads(r["block_data"]),
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in rows
            ]
        except Exception as e:
            logger.warning("get_blocks_for_document failed: %s", e)
            return []

    async def delete_section_blocks(self, document_id: str, section_key: str) -> int:
        """Borra todos los bloques de una sección (para regenerar)."""
        try:
            async with self.pool.acquire() as conn:
                result = await conn.execute("""
                    DELETE FROM document_blocks
                    WHERE document_id = $1 AND section_key = $2
                """, uuid.UUID(document_id), section_key)
            # asyncpg devuelve string tipo 'DELETE 3'
            try:
                return int(result.split()[-1])
            except Exception:
                return 0
        except Exception as e:
            logger.warning("delete_section_blocks failed: %s", e)
            return 0
