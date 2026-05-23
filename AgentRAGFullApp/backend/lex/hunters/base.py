"""Base de Hunters — multi-query parallel + re-ranking.

Cada hunter especializado hereda de HunterBase y override:
- source_filters: dict para filtrar chunks por documents.source
- doc_type_filters: list para filtrar por documents.doc_type
- corte_label: str para etiquetar resultados

El hunter base maneja:
- Embedding de query (text-embedding-3-small)
- pgvector search filtrado
- Re-rank con cross-encoder (opcional)
- Extracción de metadata (M.P., id, fecha)
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


@dataclass
class HunterHit:
    chunk_id: str
    text: str
    source: str
    doc_title: str
    similarity: float
    # Metadata específica de jurisprudencia/norma extraída
    sentencia_id: str | None = None  # 'SL1430-2022', 'C-1507/2000'
    mp: str | None = None  # 'Iván Mauricio Lenis Gómez'
    fecha: str | None = None
    corte: str | None = None
    norma_ref: str | None = None  # 'Art. 64 CST'
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "text": self.text[:1200],
            "source": self.source,
            "doc_title": self.doc_title,
            "similarity": round(self.similarity, 3),
            "id": self.sentencia_id,
            "mp": self.mp,
            "fecha": self.fecha,
            "corte": self.corte,
            "norma_ref": self.norma_ref,
        }


class HunterBase:
    """Hunter genérico. Override class vars en subclases."""

    source_filters: list[str] = []  # documents.source IN (...)
    doc_type_filters: list[str] = []  # documents.doc_type IN (...)
    corte_label: str = "general"

    # Regex para extraer M.P. del texto del chunk (best-effort)
    _MP_PATTERNS = [
        re.compile(r"M\.\s*P\.\s*:?\s*([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+){1,4})"),
        re.compile(r"[Mm]agistrad[oa]\s+[Pp]onente\s*:?\s*([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+){1,4})"),
    ]

    # Regex para extraer ID de sentencia
    _SENT_ID_PATTERNS = [
        re.compile(r"\b((?:SL|SC|SP|SU|T|C|A)-?\d{1,5}[-/]\d{2,4})\b"),
    ]

    def __init__(self, client, pool: asyncpg.Pool):
        self.client = client  # openai client
        self.pool = pool

    async def search(self, query: str, top_k: int = 3, min_similarity: float = 0.5) -> list[HunterHit]:
        """Ejecuta búsqueda en pgvector con filtros del hunter."""
        try:
            emb_resp = await self.client.embeddings.create(
                model="text-embedding-3-small",
                input=query[:8000],
            )
            qemb = emb_resp.data[0].embedding
            emb_str = "[" + ",".join(str(x) for x in qemb) + "]"

            # Construir WHERE clause dinámico
            where_clauses = ["c.embedding IS NOT NULL"]
            params: list[Any] = [emb_str, top_k * 3]  # overfetch para re-rank
            param_idx = 3
            if self.source_filters:
                where_clauses.append(f"d.source = ANY(${param_idx}::text[])")
                params.append(self.source_filters)
                param_idx += 1
            if self.doc_type_filters:
                where_clauses.append(f"d.doc_type = ANY(${param_idx}::text[])")
                params.append(self.doc_type_filters)
                param_idx += 1

            where_sql = " AND ".join(where_clauses)
            sql = f"""
                SELECT c.id::text AS chunk_id,
                       c.content AS chunk_text,
                       d.title AS doc_title,
                       d.source AS doc_source,
                       d.doc_type AS doc_type,
                       1 - (c.embedding <=> $1::vector) AS similarity
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE {where_sql}
                ORDER BY c.embedding <=> $1::vector
                LIMIT $2
            """
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(sql, *params)

            hits: list[HunterHit] = []
            for r in rows:
                sim = float(r["similarity"]) if r["similarity"] is not None else 0.0
                if sim < min_similarity:
                    continue
                text = r["chunk_text"] or ""
                hit = HunterHit(
                    chunk_id=r["chunk_id"],
                    text=text,
                    source=r["doc_source"] or "?",
                    doc_title=r["doc_title"] or "",
                    similarity=sim,
                    corte=self.corte_label,
                )
                self._enrich_metadata(hit, text)
                hits.append(hit)

            # Top-K después de filter
            return hits[:top_k]
        except Exception as e:
            logger.exception("hunter %s search failed: %s", self.__class__.__name__, e)
            return []

    def _enrich_metadata(self, hit: HunterHit, text: str) -> None:
        """Extrae M.P. e ID de sentencia del texto del chunk."""
        for pat in self._SENT_ID_PATTERNS:
            m = pat.search(text)
            if m:
                hit.sentencia_id = m.group(1)
                break

        for pat in self._MP_PATTERNS:
            m = pat.search(text)
            if m:
                hit.mp = m.group(1)
                break


async def run_hunters(
    queries: list[dict[str, Any]],  # [{query, hunter, top_k, min_similarity}, ...]
    client,
    pool: asyncpg.Pool,
) -> dict[str, list[HunterHit]]:
    """Ejecuta múltiples queries de hunters en paralelo. Devuelve dict por query."""
    from lex.hunters import get_hunter

    async def _run_one(q):
        hunter_cls = get_hunter(q.get("hunter", "general"))
        hunter = hunter_cls(client, pool)
        try:
            hits = await hunter.search(
                q["query"],
                top_k=q.get("top_k", 3),
                min_similarity=q.get("min_similarity", 0.5),
            )
            return (q["query"], hits)
        except Exception as e:
            logger.warning("hunter run failed for %s: %s", q.get("query"), e)
            return (q["query"], [])

    tasks = [_run_one(q) for q in queries]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    return dict(results)
