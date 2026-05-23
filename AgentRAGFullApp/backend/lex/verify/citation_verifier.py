"""Verifica que cada cita (norma o jurisprudencia) emitida exista en el corpus.

Para cada cita:
1. Embed la cita
2. Buscar en chunks con similarity >= threshold
3. Si encuentra → marca verified=True + chunk_id
4. Si no → marca verified=False
5. Persiste en citation_verifications
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


@dataclass
class CitationVerifyResult:
    citation_text: str
    citation_type: str  # 'norma' | 'jurisprudencia'
    verified: bool
    chunk_id: str | None = None
    similarity: float | None = None
    derogada: bool | None = None
    method: str = "rag"


class CitationVerifier:
    def __init__(self, client, pool: asyncpg.Pool, threshold: float = 0.7):
        self.client = client
        self.pool = pool
        self.threshold = threshold

    async def verify(
        self,
        citation_text: str,
        citation_type: str = "norma",
    ) -> CitationVerifyResult:
        """Busca la cita en el corpus. Si match >= threshold, marca verified."""
        try:
            emb_resp = await self.client.embeddings.create(
                model="text-embedding-3-small",
                input=citation_text[:2000],
            )
            emb_str = "[" + ",".join(str(x) for x in emb_resp.data[0].embedding) + "]"

            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT c.id::text AS chunk_id,
                           1 - (c.embedding <=> $1::vector) AS similarity
                    FROM chunks c
                    WHERE c.embedding IS NOT NULL
                    ORDER BY c.embedding <=> $1::vector
                    LIMIT 1
                """, emb_str)

            if row and row["similarity"] is not None:
                sim = float(row["similarity"])
                if sim >= self.threshold:
                    return CitationVerifyResult(
                        citation_text=citation_text,
                        citation_type=citation_type,
                        verified=True,
                        chunk_id=row["chunk_id"],
                        similarity=sim,
                    )
                return CitationVerifyResult(
                    citation_text=citation_text,
                    citation_type=citation_type,
                    verified=False,
                    chunk_id=row["chunk_id"],
                    similarity=sim,
                )
        except Exception as e:
            logger.warning("citation verify failed for %s: %s", citation_text, e)

        return CitationVerifyResult(
            citation_text=citation_text,
            citation_type=citation_type,
            verified=False,
        )

    async def verify_batch(
        self,
        citations: list[dict[str, str]],  # [{ref, type, block_id?}, ...]
        generation_id: str,
        document_id: str | None = None,
    ) -> list[CitationVerifyResult]:
        """Verifica múltiples citas en batch + persiste resultados."""
        results: list[CitationVerifyResult] = []
        for c in citations:
            r = await self.verify(c.get("ref", ""), c.get("type", "norma"))
            results.append(r)
            await self._persist(generation_id, document_id, c.get("block_id"), r)
        return results

    async def _persist(self, generation_id: str, document_id: str | None,
                       block_id: str | None, r: CitationVerifyResult) -> None:
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO citation_verifications
                        (generation_id, document_id, block_id, citation_text, citation_type,
                         chunk_id, similarity_score, verified, derogada, verification_method)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                    uuid.UUID(generation_id),
                    uuid.UUID(document_id) if document_id else None,
                    block_id, r.citation_text, r.citation_type,
                    uuid.UUID(r.chunk_id) if r.chunk_id else None,
                    r.similarity, r.verified, r.derogada, r.method)
        except Exception as e:
            logger.warning("citation_verifications insert failed: %s", e)
