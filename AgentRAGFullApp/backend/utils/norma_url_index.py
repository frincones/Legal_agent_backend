"""Sprint M18 · Cache compartido global de URLs canónicas validadas.

Tabla `norma_url_index` poblada por:
  1. Seed manual (scripts/seed_norma_url_index.py): 24 entries hardcoded
  2. SmartSearchTool (Brave Search): lazy discovery on-demand
  3. Tools live fetch (Corte CC, CSJ RSS): URL real descubierta

Cada entry persiste:
  - URL validada con GET + soft 404
  - Provenance (`discovered_by`): para auditoría legal
  - Snippet: texto que confirma la cita (mostrado al usuario)
  - Confidence: 0.0-1.0, calibrado por origen
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class IndexedNorma:
    """Entry del índice retornada por lookup."""
    fuente_url: str
    url_validated: bool
    url_http_status: Optional[int]
    titulo: Optional[str]
    snippet: Optional[str]
    vigencia: Optional[str]
    discovered_by: str
    confidence: float
    query_used: Optional[str]


async def lookup_norma_url(parsed, pool) -> Optional[IndexedNorma]:
    """Busca una norma en `norma_url_index` por su ParsedCitation.

    Returns:
        IndexedNorma si existe entry válida (url_validated=true y no expirada).
        None si miss.
    """
    if parsed is None or pool is None:
        return None

    kind = parsed.kind
    tipo = parsed.tipo
    numero = parsed.numero
    anio = parsed.anio

    if not kind or not tipo:
        return None

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT fuente_url, url_validated, url_http_status,
                       titulo, snippet, vigencia,
                       discovered_by, confidence, query_used,
                       revalidate_after
                FROM norma_url_index
                WHERE kind = $1
                  AND tipo = $2
                  AND (numero IS NOT DISTINCT FROM $3)
                  AND (anio IS NOT DISTINCT FROM $4)
                  AND url_validated = true
                  AND (revalidate_after IS NULL OR revalidate_after > now())
                LIMIT 1
                """,
                kind, tipo, numero, anio,
            )
        if not row:
            return None

        # Increment hit_count (fire-and-forget, no esperar)
        try:
            async with pool.acquire() as conn2:
                await conn2.execute(
                    """
                    UPDATE norma_url_index
                    SET hit_count = hit_count + 1,
                        last_hit_at = now()
                    WHERE kind = $1 AND tipo = $2
                      AND (numero IS NOT DISTINCT FROM $3)
                      AND (anio IS NOT DISTINCT FROM $4)
                    """,
                    kind, tipo, numero, anio,
                )
        except Exception:
            pass  # no crítico

        return IndexedNorma(
            fuente_url=row["fuente_url"],
            url_validated=bool(row["url_validated"]),
            url_http_status=row["url_http_status"],
            titulo=row["titulo"],
            snippet=row["snippet"],
            vigencia=row["vigencia"],
            discovered_by=row["discovered_by"],
            confidence=float(row["confidence"] or 0.0),
            query_used=row["query_used"],
        )
    except Exception as e:
        logger.warning("norma_url_index lookup failed: %s", e)
        return None


async def persist_norma_url(
    parsed,
    pool,
    fuente_url: str,
    *,
    discovered_by: str,
    titulo: Optional[str] = None,
    snippet: Optional[str] = None,
    vigencia: Optional[str] = None,
    url_validated: bool = False,
    url_http_status: Optional[int] = None,
    body_size_bytes: Optional[int] = None,
    confidence: float = 0.0,
    query_used: Optional[str] = None,
    revalidate_days: int = 7,
) -> bool:
    """Upsert de una norma en el índice.

    Retorna True si la operación fue exitosa.

    discovered_by: pattern|brave_search|internal_db|live_fetch|manual|llm_fallback
    """
    if parsed is None or pool is None or not fuente_url:
        return False

    kind = parsed.kind
    tipo = parsed.tipo
    if not kind or not tipo:
        return False

    normalized_ref = getattr(parsed, "normalized", None) or f"{kind}:{tipo}:{parsed.numero}/{parsed.anio}"

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO norma_url_index (
                    kind, tipo, numero, anio, normalized_ref,
                    fuente_url, url_validated, url_http_status, body_size_bytes,
                    titulo, snippet, vigencia,
                    discovered_by, query_used,
                    confidence, last_validated_at,
                    revalidate_after
                )
                VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9,
                    $10, $11, $12,
                    $13, $14,
                    $15, now(),
                    now() + ($16 || ' days')::interval
                )
                ON CONFLICT (kind, tipo, numero, anio)
                  WHERE numero IS NOT NULL AND anio IS NOT NULL
                DO UPDATE SET
                    fuente_url = EXCLUDED.fuente_url,
                    url_validated = EXCLUDED.url_validated,
                    url_http_status = EXCLUDED.url_http_status,
                    body_size_bytes = EXCLUDED.body_size_bytes,
                    titulo = COALESCE(EXCLUDED.titulo, norma_url_index.titulo),
                    snippet = COALESCE(EXCLUDED.snippet, norma_url_index.snippet),
                    vigencia = COALESCE(EXCLUDED.vigencia, norma_url_index.vigencia),
                    discovered_by = EXCLUDED.discovered_by,
                    query_used = COALESCE(EXCLUDED.query_used, norma_url_index.query_used),
                    confidence = GREATEST(EXCLUDED.confidence, norma_url_index.confidence),
                    last_validated_at = now(),
                    revalidate_after = EXCLUDED.revalidate_after,
                    validation_failures = CASE
                        WHEN EXCLUDED.url_validated THEN 0
                        ELSE norma_url_index.validation_failures + 1
                    END
                """,
                kind, tipo, parsed.numero, parsed.anio, normalized_ref,
                fuente_url, url_validated, url_http_status, body_size_bytes,
                titulo, snippet, vigencia,
                discovered_by, query_used,
                confidence, str(revalidate_days),
            )
        return True
    except Exception as e:
        # Si la unique constraint compuesta no aplicó (numero/anio null), intentar
        # con upsert simple por (kind, tipo)
        if "ON CONFLICT" in str(e) or "uq_norma_url_index" in str(e):
            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO norma_url_index (
                            kind, tipo, numero, anio, normalized_ref,
                            fuente_url, url_validated, url_http_status, body_size_bytes,
                            titulo, snippet, vigencia,
                            discovered_by, query_used,
                            confidence, last_validated_at, revalidate_after
                        )
                        VALUES (
                            $1, $2, $3, $4, $5,
                            $6, $7, $8, $9,
                            $10, $11, $12,
                            $13, $14,
                            $15, now(), now() + ($16 || ' days')::interval
                        )
                        ON CONFLICT DO NOTHING
                        """,
                        kind, tipo, parsed.numero, parsed.anio, normalized_ref,
                        fuente_url, url_validated, url_http_status, body_size_bytes,
                        titulo, snippet, vigencia,
                        discovered_by, query_used,
                        confidence, str(revalidate_days),
                    )
                return True
            except Exception as e2:
                logger.warning("norma_url_index persist fallback failed: %s", e2)
                return False
        logger.warning("norma_url_index persist failed: %s", e)
        return False
