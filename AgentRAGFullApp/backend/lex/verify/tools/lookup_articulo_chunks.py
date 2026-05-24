"""Tool: LookupArticuloChunks — ILIKE sobre chunks corpus para 'Art. X CST'.

Extrae la lógica del substring match en lex/verify/citation_verifier.py:214-242.
"""
from __future__ import annotations

import logging
import re
import time

from lex.verify.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class LookupArticuloChunks(BaseTool):
    """Busca 'artículo N' del código X en chunks corpus."""

    name = "lookup_articulo_chunks"
    timeout_seconds = 5.0

    async def run(self, parsed) -> ToolResult:
        started = time.time()
        if parsed.kind != "codigo_articulo":
            return self._build_miss(f"kind_not_codigo_articulo:{parsed.kind}")
        if parsed.numero is None:
            return self._build_miss("no_articulo_number")

        # Patterns para el artículo
        art_num = parsed.numero
        codigo = parsed.tipo
        patterns = [
            f"artículo {art_num}",
            f"articulo {art_num}",
            f"art. {art_num}",
            f"art {art_num} ",
        ]

        try:
            async with self.pool.acquire() as conn:
                where_parts = []
                params = []
                for i, p in enumerate(patterns, start=1):
                    where_parts.append(f"lower(c.content) ILIKE ${i}")
                    params.append(f"%{p}%")
                where_sql = " OR ".join(where_parts)
                # Filtrar por código si es posible (CST → documents.source LIKE %cst%)
                params.append(f"%{codigo.lower().replace('.', '')}%")
                code_filter_idx = len(params)
                sql = f"""
                    SELECT c.id::text AS chunk_id, d.title AS doc_title
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    WHERE ({where_sql})
                    AND (lower(d.title) ILIKE ${code_filter_idx} OR lower(d.source) ILIKE ${code_filter_idx})
                    LIMIT 1
                """
                row = await conn.fetchrow(sql, *params)

            if row:
                result = ToolResult(
                    tool_name=self.name,
                    status="hit",
                    confidence=0.90,
                    chunk_id=row["chunk_id"],
                    titulo=f"Art. {art_num} en {row['doc_title']}",
                    raw_evidence={"match": "ilike_in_chunks"},
                    duration_ms=int((time.time() - started) * 1000),
                )
                return self._ensure_fuente_url(result, parsed)
            # No encontrado con filtro de código — intentar sin filtro
            async with self.pool.acquire() as conn:
                row2 = await conn.fetchrow(
                    """
                    SELECT c.id::text AS chunk_id, d.title AS doc_title
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    WHERE lower(c.content) ILIKE $1
                    LIMIT 1
                    """,
                    f"%artículo {art_num}%",
                )
            if row2:
                result = ToolResult(
                    tool_name=self.name,
                    status="hit",
                    confidence=0.65,  # bajo porque no se filtró por código
                    chunk_id=row2["chunk_id"],
                    titulo=row2["doc_title"],
                    raw_evidence={"match": "ilike_no_code_filter"},
                    duration_ms=int((time.time() - started) * 1000),
                )
                return self._ensure_fuente_url(result, parsed)

            return ToolResult(
                tool_name=self.name,
                status="miss",
                confidence=0.0,
                raw_evidence={"reason": "no_match"},
                duration_ms=int((time.time() - started) * 1000),
            )
        except Exception as e:
            logger.warning("LookupArticuloChunks failed: %s", e)
            return self._build_error(e)
