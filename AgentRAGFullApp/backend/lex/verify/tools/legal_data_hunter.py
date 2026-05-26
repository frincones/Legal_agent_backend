"""Sprint M19.4 · LegalDataHunterTool — búsqueda híbrida sobre BD interna.

Equivalente local del "Legal Data Hunter" MCP de Claude: busca semánticamente
sobre nuestras tablas `jurisprudencia` + `leyes_normas` + `chunks` (RAG corpus)
usando combinación BM25 (full-text) + pgvector cuando esté disponible.

Cobertura actual (será mayor tras M19.1-M19.3 backfills):
- jurisprudencia: Corte CC + CSJ scraped previamente
- leyes_normas: datos.gov.co Senado + Función Pública seeded
- chunks: corpus RAG de documentos genéricos

Esta tool es LA PRIMERA a invocar para citas conocidas — más rápida y
sin costo externo que SmartSearchTool (Brave).
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from lex.verify.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class LegalDataHunterTool(BaseTool):
    """Búsqueda híbrida interna sobre jurisprudencia + leyes_normas."""

    name = "legal_data_hunter"
    timeout_seconds = 4.0

    async def run(self, parsed) -> ToolResult:
        started = time.time()
        if not self.pool:
            return self._build_miss("no_pool")

        try:
            # Estrategia por kind
            if parsed.kind == "jurisprudencia":
                result = await self._search_jurisprudencia(parsed, started)
            elif parsed.kind in ("ley", "decreto"):
                result = await self._search_leyes(parsed, started)
            elif parsed.kind in ("codigo", "codigo_articulo"):
                result = await self._search_codigos(parsed, started)
            else:
                return self._build_miss(f"unsupported_kind:{parsed.kind}")

            return result
        except Exception as e:
            logger.debug("LegalDataHunter failed for %s: %s",
                         getattr(parsed, "normalized", "?"), e)
            return self._build_error(e)

    async def _search_jurisprudencia(self, parsed, started) -> ToolResult:
        """Búsqueda directa por tipo+numero+anio en tabla jurisprudencia."""
        tipo = parsed.tipo
        numero = parsed.numero
        anio = parsed.anio
        if not (tipo and numero):
            return self._build_miss("incomplete_jurisp")

        async with self.pool.acquire() as conn:
            # Match exacto por providencia (formato T-XXX/YY, SU-XXX/YY, SLXXX-YYYY)
            providencia_patterns = [
                f"{tipo}-{numero}/{str(anio)[-2:]}" if anio else f"{tipo}-{numero}",
                f"{tipo}-{numero:03d}/{str(anio)[-2:]}" if anio else None,
                f"{tipo}{numero}-{anio}" if anio else None,
                f"{tipo}{numero}/{anio}" if anio else None,
                f"{tipo} {numero} de {anio}" if anio else None,
            ]
            providencia_patterns = [p for p in providencia_patterns if p]

            for pat in providencia_patterns:
                row = await conn.fetchrow(
                    """
                    SELECT id::text as juris_id, providencia, rubro,
                           magistrado_ponente, fuente_url, fecha_decision
                    FROM jurisprudencia
                    WHERE providencia ILIKE $1
                    LIMIT 1
                    """,
                    pat,
                )
                if row:
                    return ToolResult(
                        tool_name=self.name,
                        status="hit",
                        confidence=0.94,
                        fuente_url=row["fuente_url"],
                        titulo=row["rubro"] or row["providencia"],
                        chunk_id=row["juris_id"],
                        raw_evidence={
                            "discovered_by": "internal_db",
                            "match": "providencia_ilike",
                            "magistrado": row["magistrado_ponente"],
                            "fecha": str(row["fecha_decision"]) if row["fecha_decision"] else None,
                            "snippet": (row["rubro"] or "")[:300],
                        },
                        duration_ms=int((time.time() - started) * 1000),
                    )

        return self._build_miss("no_jurisp_match")

    async def _search_leyes(self, parsed, started) -> ToolResult:
        """Búsqueda en leyes_normas por (tipo, numero, anio)."""
        numero = parsed.numero
        anio = parsed.anio
        if not (numero and anio):
            return self._build_miss("incomplete_ley")

        # Mapear tipo a tipo_norma de la tabla
        tipo_db = "ley" if parsed.kind == "ley" else "decreto"

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id::text as norma_id, citation_ref, titulo,
                       fuente_url, vigente, derogada_por_ref
                FROM leyes_normas
                WHERE tipo_norma = $1
                  AND numero = $2
                  AND anio = $3
                ORDER BY fetched_at DESC NULLS LAST
                LIMIT 1
                """,
                tipo_db, str(numero), anio,
            )
            if row and row["fuente_url"]:
                return ToolResult(
                    tool_name=self.name,
                    status="hit",
                    confidence=0.95,
                    fuente_url=row["fuente_url"],
                    titulo=row["titulo"] or row["citation_ref"],
                    chunk_id=row["norma_id"],
                    raw_evidence={
                        "discovered_by": "internal_db",
                        "match": "exact_norma",
                        "vigente": row["vigente"],
                        "derogada_por": row["derogada_por_ref"],
                        "snippet": (row["titulo"] or "")[:300],
                    },
                    duration_ms=int((time.time() - started) * 1000),
                )

        return self._build_miss("no_ley_match")

    async def _search_codigos(self, parsed, started) -> ToolResult:
        """Búsqueda en chunks corpus (RAG) por alias del código."""
        tipo = parsed.tipo
        numero = parsed.numero
        if not tipo:
            return self._build_miss("no_tipo_codigo")

        # Match por título de documento (CST, C.P., etc.)
        async with self.pool.acquire() as conn:
            # Buscar primero por documents.title que contenga el código
            row = await conn.fetchrow(
                """
                SELECT d.id::text as doc_id, d.title, d.source
                FROM documents d
                WHERE lower(d.title) ILIKE $1
                   OR lower(d.title) ILIKE $2
                   OR lower(d.source) ILIKE $1
                LIMIT 1
                """,
                f"%{tipo.lower().replace('.','')}%",
                f"%{tipo.lower()}%",
            )
            if row:
                # Snippet de un chunk del doc si hay article number
                snippet = None
                if numero:
                    chunk_row = await conn.fetchrow(
                        """
                        SELECT substring(content, 1, 300) as snippet
                        FROM chunks
                        WHERE document_id = $1::uuid
                          AND content ILIKE $2
                        LIMIT 1
                        """,
                        row["doc_id"], f"%art%culo {numero}%",
                    )
                    if chunk_row:
                        snippet = chunk_row["snippet"]

                return ToolResult(
                    tool_name=self.name,
                    status="hit",
                    confidence=0.88,
                    titulo=row["title"],
                    chunk_id=row["doc_id"],
                    raw_evidence={
                        "discovered_by": "internal_db",
                        "match": "documents_title",
                        "snippet": snippet or row["title"][:300],
                    },
                    duration_ms=int((time.time() - started) * 1000),
                )

        return self._build_miss("no_codigo_match")
