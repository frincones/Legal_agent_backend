"""VerificationAgent — orquestador del flow de verificación de citas.

Pipeline:
1. NormalizationLayer (parse_citation_ref usa expand_to_canonical de M13)
2. CacheGate (external_fetch_cache, TTL por kind)
3. ToolDispatcher (decide qué tools invocar)
4. EvidenceAccumulator (combina resultados)
5. PersistenceGate (cache + audit en verification_attempts)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from lex.verify.evidence_accumulator import EvidenceAccumulator, VerificationVerdict
from lex.verify.tool_dispatcher import ToolDispatcher

logger = logging.getLogger(__name__)


# TTL del cache por kind (segundos)
TTL_BY_KIND = {
    "jurisprudencia": 30 * 24 * 3600,   # 30 días
    "ley": 7 * 24 * 3600,                # 7 días
    "decreto": 7 * 24 * 3600,
    "codigo": 30 * 24 * 3600,
    "codigo_articulo": 30 * 24 * 3600,
}


def _build_cache_key(parsed) -> str:
    parts = ["v1", parsed.kind]
    if parsed.tipo:
        parts.append(parsed.tipo.upper())
    if parsed.numero is not None:
        parts.append(str(parsed.numero))
    if parsed.anio is not None:
        parts.append(str(parsed.anio))
    return ":".join(parts)


class VerificationAgent:
    """Verificador de citas con cascada determinística + cache."""

    def __init__(self, client, pool, firm_id=None, user_id=None, max_concurrent: int = 4):
        self.client = client
        self.pool = pool
        self.firm_id = firm_id
        self.user_id = user_id
        self.dispatcher = ToolDispatcher(pool=pool, client=client, max_concurrent=max_concurrent)
        self.accumulator = EvidenceAccumulator()

    async def verify(self, citation_text: str, citation_type: str = "norma") -> VerificationVerdict:
        """Verifica una sola cita. Retorna verdict completo."""
        started = time.time()

        # 1. NORMALIZATION
        from utils.citation_verifier import parse_citation_ref
        parsed = parse_citation_ref(citation_text)

        # M15: si el parser regex falla, intentar LLM normalizer como último recurso
        if not parsed and self.client is not None:
            try:
                from lex.verify.llm_normalizer import llm_normalize_citation
                llm_result = await llm_normalize_citation(citation_text, self.client, self.pool)
                if llm_result and llm_result.get("kind") not in (None, "unknown"):
                    # Re-construir cita canónica y re-parsear
                    kind = llm_result["kind"]
                    tipo = llm_result.get("tipo", "")
                    numero = llm_result.get("numero")
                    anio = llm_result.get("anio")
                    if kind == "jurisprudencia" and tipo and numero and anio:
                        reconstructed = f"{tipo}-{numero}/{anio}"
                    elif kind in ("ley", "decreto") and numero and anio:
                        reconstructed = f"{tipo or kind.upper()} {numero}/{anio}"
                    elif kind == "codigo_articulo" and tipo and numero:
                        reconstructed = f"Art. {numero} {tipo}"
                    elif kind == "codigo" and tipo:
                        reconstructed = tipo
                    else:
                        reconstructed = None
                    if reconstructed:
                        parsed = parse_citation_ref(reconstructed)
                        if parsed:
                            logger.info("LLM normalizer recovered %r -> %r", citation_text, reconstructed)
            except Exception as e:
                logger.warning("LLM normalizer fallback failed: %s", e)

        if not parsed:
            return VerificationVerdict(
                citation_text=citation_text,
                citation_type=citation_type,
                estado="no_encontrada",
                verified=False,
                confidence=0.0,
                method="unparseable",
                duration_ms=int((time.time() - started) * 1000),
            )

        # 2. CACHE GATE
        cache_key = _build_cache_key(parsed)
        cached = await self._cache_get(cache_key)
        if cached:
            cached_verdict = self._verdict_from_cache(cached, citation_text, citation_type)
            cached_verdict.duration_ms = int((time.time() - started) * 1000)
            return cached_verdict

        # 3. TOOL DISPATCH
        tools = self.dispatcher.dispatch(parsed)
        tool_results = await self.dispatcher.execute_all(parsed, tools)

        # 4. EVIDENCE ACCUMULATOR
        evidence = self.accumulator.collect(citation_text, tool_results)
        verdict = self.accumulator.compute_verdict(evidence, citation_type)
        verdict.duration_ms = int((time.time() - started) * 1000)

        # 5. PERSISTENCE
        await self._persist(parsed, cache_key, verdict, tool_results)

        return verdict

    async def verify_batch(self, citations: list[dict]) -> list[VerificationVerdict]:
        """Verifica batch de citas en paralelo.

        citations: list de dicts con keys {ref, type, block_id?}
        """
        async def _one(cit):
            return await self.verify(
                citation_text=cit.get("ref", ""),
                citation_type=cit.get("type", "norma"),
            )
        return await asyncio.gather(*[_one(c) for c in citations])

    async def _cache_get(self, key: str) -> dict | None:
        """Lookup en external_fetch_cache."""
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT content_jsonb FROM external_fetch_cache
                    WHERE cache_key = $1
                      AND fetched_at + (ttl_seconds || ' seconds')::interval > now()
                      AND status = 'ok'
                    """,
                    key,
                )
            if row and row["content_jsonb"]:
                d = row["content_jsonb"]
                if isinstance(d, str):
                    d = json.loads(d)
                return d
        except Exception as e:
            logger.warning("cache_get failed: %s", e)
        return None

    async def _persist(self, parsed, cache_key: str, verdict: VerificationVerdict,
                       tool_results: list) -> None:
        """Persist a external_fetch_cache + verification_attempts."""
        # Cache
        try:
            ttl = TTL_BY_KIND.get(parsed.kind, 24 * 3600)
            cache_payload = {
                "estado": verdict.estado,
                "verified": verdict.verified,
                "confidence": verdict.confidence,
                "method": verdict.method,
                "fuente_url": verdict.fuente_url,
                "titulo": verdict.titulo,
                "chunk_id": verdict.chunk_id,
                "derogada": verdict.derogada,
            }
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO external_fetch_cache
                      (cache_key, source, content_jsonb, status, ttl_seconds)
                    VALUES ($1, 'verification_agent', $2::jsonb, 'ok', $3)
                    ON CONFLICT (cache_key) DO UPDATE
                      SET content_jsonb = EXCLUDED.content_jsonb,
                          fetched_at = now(),
                          ttl_seconds = EXCLUDED.ttl_seconds,
                          hit_count = external_fetch_cache.hit_count + 1,
                          last_hit_at = now()
                    """,
                    cache_key,
                    json.dumps(cache_payload, ensure_ascii=False, default=str),
                    ttl,
                )
        except Exception as e:
            logger.warning("cache_persist failed: %s", e)

        # Audit
        try:
            tool_results_json = [
                {
                    "tool": t.tool_name,
                    "status": t.status,
                    "confidence": t.confidence,
                    "duration_ms": t.duration_ms,
                    "fuente_url": t.fuente_url,
                    "error": t.error_message,
                }
                for t in tool_results
            ]
            sources_jsonb = json.dumps(verdict.sources_tried, ensure_ascii=False)
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO verification_attempts
                      (firm_id, user_id, citation_ref, ref_type, result_state,
                       source, duration_ms, confidence_score, sources_tried,
                       normalized_ref, tool_results)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11::jsonb)
                    """,
                    uuid.UUID(self.firm_id) if self.firm_id else None,
                    uuid.UUID(self.user_id) if self.user_id else None,
                    verdict.citation_text,
                    verdict.citation_type,
                    verdict.estado,
                    verdict.method,
                    verdict.duration_ms,
                    verdict.confidence,
                    sources_jsonb,
                    parsed.normalized,
                    json.dumps(tool_results_json, ensure_ascii=False, default=str),
                )
        except Exception as e:
            logger.warning("audit_persist failed: %s", e)

    def _verdict_from_cache(self, cached: dict, citation_text: str,
                            citation_type: str) -> VerificationVerdict:
        return VerificationVerdict(
            citation_text=citation_text,
            citation_type=citation_type,
            estado=cached.get("estado", "no_encontrada"),
            verified=cached.get("verified", False),
            confidence=cached.get("confidence", 0.0),
            method=f"cache:{cached.get('method', 'unknown')}",
            fuente_url=cached.get("fuente_url"),
            titulo=cached.get("titulo"),
            chunk_id=cached.get("chunk_id"),
            derogada=cached.get("derogada", False),
            sources_tried=["cache"],
        )
