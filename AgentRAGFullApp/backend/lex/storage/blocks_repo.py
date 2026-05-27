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

    # M19.16.B2 — Harvey-style inline edits
    async def update_block(
        self,
        document_id: str,
        block_id: str,
        new_block_data: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Actualiza block_data de un bloque puntual. Devuelve la fila actualizada o None
        si no existe el bloque.

        Garantiza que solo se actualiza si (document_id, block_id) coincide,
        evitando que un usuario edite bloques de otro documento.
        """
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    UPDATE document_blocks
                       SET block_data = $1::jsonb
                     WHERE document_id = $2 AND block_id = $3
                 RETURNING block_id, section_key, block_order, block_type, block_data
                """, json.dumps(new_block_data, ensure_ascii=False, default=str),
                     uuid.UUID(document_id), block_id)
        except Exception as e:
            logger.warning("update_block failed for %s/%s: %s", document_id, block_id, e)
            return None
        if row is None:
            return None
        return {
            "block_id": row["block_id"],
            "section_key": row["section_key"],
            "block_order": row["block_order"],
            "block_type": row["block_type"],
            "block_data": row["block_data"] if isinstance(row["block_data"], dict)
                          else json.loads(row["block_data"]),
        }

    async def replace_block_runs(
        self,
        document_id: str,
        block_id: str,
        new_runs: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Helper: reemplaza solo el array `runs` dentro de block_data. Usado por chat
        actions `update_block` y por edits inline cuando el usuario solo cambia texto.

        Devuelve la fila actualizada o None si el bloque no existe o no tiene `runs`.
        """
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT block_data FROM document_blocks
                     WHERE document_id = $1 AND block_id = $2
                """, uuid.UUID(document_id), block_id)
                if row is None:
                    return None
                bd = row["block_data"] if isinstance(row["block_data"], dict) \
                     else json.loads(row["block_data"])
                bd["runs"] = new_runs
                updated = await conn.fetchrow("""
                    UPDATE document_blocks
                       SET block_data = $1::jsonb
                     WHERE document_id = $2 AND block_id = $3
                 RETURNING block_id, section_key, block_order, block_type, block_data
                """, json.dumps(bd, ensure_ascii=False, default=str),
                     uuid.UUID(document_id), block_id)
            if updated is None:
                return None
            return {
                "block_id": updated["block_id"],
                "section_key": updated["section_key"],
                "block_order": updated["block_order"],
                "block_type": updated["block_type"],
                "block_data": updated["block_data"] if isinstance(updated["block_data"], dict)
                              else json.loads(updated["block_data"]),
            }
        except Exception as e:
            logger.warning("replace_block_runs failed for %s/%s: %s", document_id, block_id, e)
            return None

    # M19.19.A — insertar un bloque nuevo después de un block_id existente
    async def insert_block_after(
        self,
        document_id: str,
        after_block_id: str,
        block_id: str,
        block_type: str,
        block_data: dict[str, Any],
        section_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Inserta un bloque nuevo INMEDIATAMENTE después de `after_block_id`.

        Reordena `block_order` de los bloques posteriores (+1) en una sola
        transacción. Devuelve la fila del bloque insertado, o None si el
        after_block_id no existe.

        Si `section_key` es None, hereda la sección del bloque ancla.
        """
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    anchor = await conn.fetchrow("""
                        SELECT generation_id, section_key, block_order
                        FROM document_blocks
                        WHERE document_id = $1::uuid AND block_id = $2
                    """, document_id, after_block_id)
                    if anchor is None:
                        return None
                    target_section = section_key or anchor["section_key"]
                    target_order = anchor["block_order"] + 1
                    gen_id = anchor["generation_id"]
                    # Shift +1 a todos los bloques con block_order >= target_order
                    await conn.execute("""
                        UPDATE document_blocks
                        SET block_order = block_order + 1
                        WHERE document_id = $1::uuid AND block_order >= $2
                    """, document_id, target_order)
                    # Insertar el nuevo bloque
                    row = await conn.fetchrow("""
                        INSERT INTO document_blocks
                            (document_id, generation_id, section_key, block_order,
                             block_id, block_type, block_data)
                        VALUES ($1::uuid, $2, $3, $4, $5, $6, $7::jsonb)
                        RETURNING block_id, section_key, block_order, block_type, block_data
                    """,
                        document_id, gen_id, target_section, target_order,
                        block_id, block_type,
                        json.dumps(block_data, ensure_ascii=False, default=str),
                    )
            return {
                "block_id": row["block_id"],
                "section_key": row["section_key"],
                "block_order": row["block_order"],
                "block_type": row["block_type"],
                "block_data": row["block_data"] if isinstance(row["block_data"], dict)
                              else json.loads(row["block_data"]),
            }
        except Exception as e:
            logger.warning("insert_block_after failed for %s/%s: %s", document_id, after_block_id, e)
            return None

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
