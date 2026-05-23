"""Verifica que cada cita (norma o jurisprudencia) emitida exista en el corpus.

Estrategia híbrida (Sprint M9):
1. SUBSTRING MATCH primero (más rápido y preciso para citas cortas como "Art. 64 CST"):
   - Construye variantes ortográficas de la cita
   - Busca con ILIKE en chunks.content + documents.title
   - Si match exacto → verified=True, similarity=1.0, method="substring"
2. EMBEDDING SIMILARITY como fallback (para citas que no aparezcan textuales):
   - Embed query, search pgvector
   - Threshold 0.5 (más permisivo porque ya pasó por substring)
3. Si ninguno → verified=False
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


@dataclass
class CitationVerifyResult:
    citation_text: str
    citation_type: str
    verified: bool
    chunk_id: str | None = None
    similarity: float | None = None
    derogada: bool | None = None
    method: str = "rag"


def _build_search_patterns(citation_text: str, citation_type: str) -> list[str]:
    """Construye variantes ortográficas para buscar la cita en el corpus.

    Ejemplos:
      "Art. 64 CST"   → ["art. 64", "artículo 64", "art 64 cst", "código sustantivo del trabajo"]
      "SL1430-2022"   → ["sl1430-2022", "sl1430 2022", "sl 1430"]
      "C-1507/2000"   → ["c-1507/2000", "c-1507", "sentencia c-1507"]
      "Ley 50 de 1990" → ["ley 50 de 1990", "ley 50/1990", "ley 50/90"]
    """
    text = citation_text.lower().strip()
    patterns: list[str] = [text]

    if citation_type == "norma":
        m_art = re.search(r"art\.?\s*(\d+)", text)
        if m_art:
            num = m_art.group(1)
            patterns.append(f"art. {num}")
            patterns.append(f"artículo {num}")
            patterns.append(f"articulo {num}")
        m_ley = re.search(r"ley\s+(\d+)\s+de\s+(\d{2,4})", text)
        if m_ley:
            n, y = m_ley.group(1), m_ley.group(2)
            patterns.append(f"ley {n}/{y[-2:]}")
            patterns.append(f"ley {n}/{y}")
            patterns.append(f"ley {n} de {y}")
        m_dec = re.search(r"decreto\s+(\d+)\s+de\s+(\d{2,4})", text)
        if m_dec:
            patterns.append(f"decreto {m_dec.group(1)}")
    else:  # jurisprudencia
        m_sent = re.search(r"(sl|sc|sp|su|t|c|a)[\s\-]*(\d{1,5})[/\-](\d{2,4})", text)
        if m_sent:
            pre, num, yr = m_sent.group(1), m_sent.group(2), m_sent.group(3)
            patterns.append(f"{pre}{num}-{yr}")
            patterns.append(f"{pre}-{num}-{yr}")
            patterns.append(f"{pre}-{num}/{yr}")
            patterns.append(f"sentencia {pre}{num}")
            patterns.append(f"sentencia {pre}-{num}")

    # Dedup mantenedo orden
    seen = set()
    out = []
    for p in patterns:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


class CitationVerifier:
    def __init__(self, client, pool: asyncpg.Pool, threshold: float = 0.5):
        self.client = client
        self.pool = pool
        self.threshold = threshold  # bajado a 0.5 (sprint M9)

    async def verify(
        self,
        citation_text: str,
        citation_type: str = "norma",
    ) -> CitationVerifyResult:
        """Verifica vía substring match primero, embedding como fallback."""
        # 1) Substring match (ILIKE) sobre chunks.content + documents.title
        patterns = _build_search_patterns(citation_text, citation_type)
        try:
            async with self.pool.acquire() as conn:
                # OR de todos los patterns: cada uno con ILIKE
                where_parts = []
                params: list[Any] = []
                for i, p in enumerate(patterns[:8], start=1):
                    where_parts.append(f"(lower(c.content) ILIKE ${i} OR lower(d.title) ILIKE ${i})")
                    params.append(f"%{p}%")
                where_sql = " OR ".join(where_parts)
                sql = f"""
                    SELECT c.id::text AS chunk_id, d.title AS title
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    WHERE {where_sql}
                    LIMIT 1
                """
                row = await conn.fetchrow(sql, *params)
            if row:
                return CitationVerifyResult(
                    citation_text=citation_text,
                    citation_type=citation_type,
                    verified=True,
                    chunk_id=row["chunk_id"],
                    similarity=1.0,
                    method="substring",
                )
        except Exception as e:
            logger.warning("substring match failed for %s: %s", citation_text, e)

        # 2) Fallback embedding similarity
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
                        method="embedding",
                    )
                return CitationVerifyResult(
                    citation_text=citation_text,
                    citation_type=citation_type,
                    verified=False,
                    chunk_id=row["chunk_id"],
                    similarity=sim,
                    method="embedding",
                )
        except Exception as e:
            logger.warning("embedding verify failed for %s: %s", citation_text, e)

        return CitationVerifyResult(
            citation_text=citation_text,
            citation_type=citation_type,
            verified=False,
            method="fallback",
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
