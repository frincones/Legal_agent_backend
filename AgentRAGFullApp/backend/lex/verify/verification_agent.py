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
from lex.verify.judge_agent import JudgeAgent

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
    # m18 prefix: invalida cache (M18: index lookup + judge + provenance)
    parts = ["m18", parsed.kind]
    if parsed.tipo:
        parts.append(parsed.tipo.upper())
    if parsed.numero is not None:
        parts.append(str(parsed.numero))
    if parsed.anio is not None:
        parts.append(str(parsed.anio))
    return ":".join(parts)


class VerificationAgent:
    """Verificador de citas con cascada determinística + cache."""

    def __init__(self, client, pool, firm_id=None, user_id=None, max_concurrent: int = 4,
                 on_thought=None):
        """
        Args:
            on_thought: callable(message, kind, **kwargs) opcional para narration
                       en vivo. Si None, no emite eventos.
        """
        self.client = client
        self.pool = pool
        self.firm_id = firm_id
        self.user_id = user_id
        self.dispatcher = ToolDispatcher(pool=pool, client=client, max_concurrent=max_concurrent)
        self.accumulator = EvidenceAccumulator()
        # M18.d: callback de narration en vivo (estilo Claude)
        self._on_thought = on_thought
        # M18: Judge para validación adversarial. Disabled si no hay client.
        # Se puede deshabilitar globalmente con JUDGE_AGENT_ENABLED=false
        import os
        judge_env = os.getenv("JUDGE_AGENT_ENABLED", "true").lower()
        judge_enabled = (judge_env in ("true", "1", "yes")) and (client is not None)
        self.judge = JudgeAgent(client=client, enabled=judge_enabled)

    def _thought(self, message: str, kind: str = "info", **extra):
        """Emite un agent_thought si hay callback registrado. Safe no-op si None."""
        cb = self._on_thought
        if cb is None:
            return
        try:
            cb(message=message, kind=kind, **extra)
        except Exception as e:
            logger.debug("on_thought callback failed: %s", e)

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

        # 2. CACHE GATE (verdict cache TTL por kind)
        cache_key = _build_cache_key(parsed)
        cached = await self._cache_get(cache_key)
        if cached:
            cached_verdict = self._verdict_from_cache(cached, citation_text, citation_type)
            cached_verdict.duration_ms = int((time.time() - started) * 1000)
            return cached_verdict

        # 3. TOOL DISPATCH
        tools = self.dispatcher.dispatch(parsed)

        # M19.5: callback que emite tool_call event con request/response al frontend
        async def _emit_tool_event(tool_name, tool_id, status, request, response, error, duration_ms):
            # Composición del mensaje del thought (corto, el frontend renderiza el chip)
            label = tool_name.replace("_", " ")
            if status == "running":
                msg = f"Llamando {label}..."
            elif status == "done":
                msg = f"{label} completado en {duration_ms}ms"
            else:
                msg = f"{label} falló: {error or 'error'}"
            self._thought(
                msg, kind="tool_call",
                tool=tool_name, ref=citation_text,
                tool_id=tool_id,
                tool_request=request,
                tool_response=response,
                tool_error=error,
                tool_duration_ms=duration_ms,
            )

        tool_results = await self.dispatcher.execute_all(parsed, tools, on_tool=_emit_tool_event)

        # 4. EVIDENCE ACCUMULATOR
        evidence = self.accumulator.collect(citation_text, tool_results)
        verdict = self.accumulator.compute_verdict(evidence, citation_type)
        verdict.duration_ms = int((time.time() - started) * 1000)

        # 4.1 M18: propagar provenance + snippet del best_hit al verdict
        # (Los tool events individuales ya se emitieron en dispatcher.execute_all
        # via on_tool callback con request/response. No duplicar narration aquí.)
        best_hit = evidence.best_hit()
        if best_hit:
            verdict.discovered_by = best_hit.discovered_by
            verdict.snippet = best_hit.snippet
            verdict.query_used = best_hit.query_used

        # 5. M17 GUARANTEE FUENTE_URL + HEAD CASCADE
        # Estrategia: para cada cita verificada, probar TODOS los candidatos
        # canónicos con HEAD hasta encontrar uno con HTTP 200. Si ninguno
        # responde, último recurso = búsqueda Google (siempre responde).
        if verdict.verified:
            try:
                from utils.citation_url_builder import (
                    build_url_candidates,
                    build_search_fallback_url,
                )
                from utils.url_validator import find_valid_url

                candidates: list[str] = []
                # Si la tool ya trajo una URL (live fetch / BD), va primero
                if verdict.fuente_url:
                    candidates.append(verdict.fuente_url)
                # Luego los candidatos canónicos en orden de prioridad
                for c in build_url_candidates(parsed):
                    if c not in candidates:
                        candidates.append(c)

                if candidates:
                    valid_url, status_code = await find_valid_url(candidates, self.pool)
                    verdict.url_http_status = status_code
                    if valid_url:
                        verdict.fuente_url = valid_url
                        verdict.url_validated = True
                    else:
                        # Ningún candidato responde → fallback Google (no marca como validada)
                        verdict.fuente_url = build_search_fallback_url(parsed)
                        verdict.url_validated = False
                        logger.warning(
                            "HEAD failed for ALL %d candidates of %s -> search fallback",
                            len(candidates), citation_text,
                        )
                else:
                    # No hay candidatos -> Google search directo
                    verdict.fuente_url = build_search_fallback_url(parsed)
                    verdict.url_validated = False
            except Exception as e:
                logger.warning("URL cascade failed for %s: %s", citation_text, e)

        # 6. M17 DEROGADA: construir fuente_url_vigente si superada
        verdict.fuente_url_original = verdict.fuente_url
        if verdict.estado == "superada":
            try:
                from utils.citation_url_builder import build_url_candidates
                from utils.url_validator import find_valid_url
                from utils.citation_verifier import parse_citation_ref
                # Buscar derogada_por en check_derogation evidence
                for tr in tool_results:
                    if tr.tool_name == "check_derogation":
                        derogada_por = tr.raw_evidence.get("derogada_por") if tr.raw_evidence else None
                        if derogada_por:
                            parsed_vigente = parse_citation_ref(derogada_por)
                            if parsed_vigente:
                                cands_v = build_url_candidates(parsed_vigente)
                                valid_v, _ = await find_valid_url(cands_v, self.pool)
                                verdict.fuente_url_vigente = valid_v or (cands_v[0] if cands_v else None)
                            break
            except Exception as e:
                logger.warning("derogada vigente URL failed: %s", e)

        # 7. M18: JUDGE AGENT — validación adversarial del verdict
        if verdict.verified and self.judge.enabled:
            try:
                judge_out = await self.judge.judge(
                    citation_text=citation_text,
                    parsed=parsed,
                    verdict=verdict,
                    tool_results=tool_results,
                )
                verdict.judge_action = judge_out.action
                verdict.judge_rationale = judge_out.rationale
                # M18.c: propagar suggested_correction + legal_note al verdict
                verdict.suggested_correction = judge_out.suggested_correction
                verdict.legal_note = judge_out.legal_note

                # M18.d: narration de hallazgos del Judge
                if judge_out.suggested_correction:
                    self._thought(
                        f"  💡 Sugerencia legal: '{citation_text}' parece incorrecta. ¿Quizás '{judge_out.suggested_correction}'?",
                        kind="correction", ref=citation_text,
                        suggestion=judge_out.suggested_correction,
                    )
                if judge_out.legal_note:
                    self._thought(
                        f"  ⚖ Nota legal: {judge_out.legal_note}",
                        kind="warning", ref=citation_text,
                    )

                if judge_out.action == "reject":
                    # Judge rechaza el verdict → marcar como no encontrada
                    logger.info(
                        "Judge REJECTED %r: %s",
                        citation_text, judge_out.rationale,
                    )
                    verdict.estado = "no_encontrada"
                    verdict.verified = False
                    verdict.confidence = min(verdict.confidence, 0.3)
                elif judge_out.action == "refine" and judge_out.next_query and not verdict.judge_retried:
                    # Re-buscar con next_query usando SmartSearchTool
                    logger.info(
                        "Judge requested REFINE for %r: query=%r",
                        citation_text, judge_out.next_query[:80],
                    )
                    verdict.judge_retried = True
                    try:
                        from lex.verify.tools.smart_search import SmartSearchTool
                        # Construir parsed temporal con normalized override
                        smart = SmartSearchTool(pool=self.pool, client=self.client)
                        # Reusar mismo parsed pero con query custom inyectado en raw_evidence
                        # (smart_search lo recibe via parsed.normalized si está set)
                        original_normalized = getattr(parsed, "normalized", None)
                        try:
                            parsed.normalized = judge_out.next_query
                        except Exception:
                            pass
                        retry_result = await smart.run(parsed)
                        # Restore
                        try:
                            if original_normalized is not None:
                                parsed.normalized = original_normalized
                        except Exception:
                            pass

                        if retry_result.is_hit and retry_result.fuente_url:
                            # Validar URL del retry
                            from utils.url_validator import validate_url_responsive
                            is_valid, status_code = await validate_url_responsive(
                                retry_result.fuente_url, self.pool
                            )
                            if is_valid:
                                verdict.fuente_url = retry_result.fuente_url
                                verdict.titulo = retry_result.titulo or verdict.titulo
                                verdict.snippet = retry_result.snippet or verdict.snippet
                                verdict.discovered_by = retry_result.discovered_by
                                verdict.url_validated = True
                                verdict.url_http_status = status_code
                                verdict.query_used = judge_out.next_query
                                tool_results.append(retry_result)
                    except Exception as e:
                        logger.warning("judge refine retry failed: %s", e)
                else:
                    # action=accept → ajustar confidence con el del Judge si difiere
                    if abs(judge_out.confidence_adjusted - verdict.confidence) > 0.05:
                        verdict.confidence = judge_out.confidence_adjusted
            except Exception as e:
                logger.warning("JudgeAgent stage failed (continuing): %s", e)

        # 7.5 M18: persistir en norma_url_index si tenemos URL validada
        if verdict.url_validated and verdict.fuente_url and verdict.verified:
            try:
                from utils.norma_url_index import persist_norma_url
                await persist_norma_url(
                    parsed=parsed,
                    pool=self.pool,
                    fuente_url=verdict.fuente_url,
                    discovered_by=verdict.discovered_by or "verification_agent",
                    titulo=verdict.titulo,
                    snippet=verdict.snippet,
                    vigencia="derogada" if verdict.derogada else "vigente",
                    url_validated=True,
                    url_http_status=verdict.url_http_status,
                    confidence=verdict.confidence,
                    query_used=verdict.query_used,
                    revalidate_days=(90 if verdict.derogada else 7),
                )
            except Exception as e:
                logger.debug("norma_url_index persist (post-verdict) failed: %s", e)

        # 8. PERSISTENCE
        await self._persist(parsed, cache_key, verdict, tool_results)

        return verdict

    async def verify_batch(self, citations: list[dict]) -> list[VerificationVerdict]:
        """Verifica batch de citas en paralelo.

        M18.c: dedup por (ref, type). Si la misma cita aparece N veces
        (ej. "Art. 64 CST" repetido en 5 bloques), solo verifica 1 vez
        y retorna el mismo verdict para todas las apariciones.

        citations: list de dicts con keys {ref, type, block_id?}
        """
        # 1) Identificar citas únicas
        seen: dict[tuple[str, str], int] = {}    # (ref, type) → idx único
        unique_to_verify: list[dict] = []
        index_map: list[int] = []                # citations[i] → unique_idx
        for cit in citations:
            ref = (cit.get("ref") or "").strip()
            ctype = (cit.get("type") or "norma").strip()
            key = (ref, ctype)
            if key not in seen:
                seen[key] = len(unique_to_verify)
                unique_to_verify.append(cit)
            index_map.append(seen[key])

        # 2) Verificar SOLO las únicas en paralelo
        async def _one(cit):
            return await self.verify(
                citation_text=cit.get("ref", ""),
                citation_type=cit.get("type", "norma"),
            )
        unique_verdicts = await asyncio.gather(*[_one(c) for c in unique_to_verify])

        # 3) Reconstruir lista completa reusando verdicts (dedup ratio log)
        if len(citations) > len(unique_to_verify):
            logger.info(
                "verify_batch dedup: %d citas -> %d únicas (-%d redundantes)",
                len(citations), len(unique_to_verify),
                len(citations) - len(unique_to_verify),
            )
        return [unique_verdicts[idx] for idx in index_map]

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
                # M17
                "fuente_url_original": verdict.fuente_url_original,
                "fuente_url_vigente": verdict.fuente_url_vigente,
                "url_http_status": verdict.url_http_status,
                "url_validated": verdict.url_validated,
                # M18
                "discovered_by": verdict.discovered_by,
                "snippet": verdict.snippet,
                "judge_action": verdict.judge_action,
                "judge_rationale": verdict.judge_rationale,
                "judge_retried": verdict.judge_retried,
                "query_used": verdict.query_used,
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
        # M18.c FIX: el check constraint ref_type acepta solo
        # ('jurisprudencia','ley','decreto','codigo'). Mapear 'norma',
        # 'codigo_articulo' o vacíos al equivalente válido para evitar
        # violaciones de check constraint que rompen el INSERT.
        try:
            VALID_REF_TYPES = {"jurisprudencia", "ley", "decreto", "codigo"}
            # Preferir parsed.kind (más específico) → mapear a valor válido
            raw_kind = (parsed.kind or "").lower() if parsed else ""
            if raw_kind == "codigo_articulo":
                ref_type_db = "codigo"
            elif raw_kind in VALID_REF_TYPES:
                ref_type_db = raw_kind
            elif verdict.citation_type and verdict.citation_type.lower() in VALID_REF_TYPES:
                ref_type_db = verdict.citation_type.lower()
            else:
                # Fallback seguro
                ref_type_db = "ley"

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
                    ref_type_db,
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
            # M17
            fuente_url_original=cached.get("fuente_url_original"),
            fuente_url_vigente=cached.get("fuente_url_vigente"),
            url_http_status=cached.get("url_http_status"),
            url_validated=cached.get("url_validated", False),
            # M18
            discovered_by=cached.get("discovered_by"),
            snippet=cached.get("snippet"),
            judge_action=cached.get("judge_action"),
            judge_rationale=cached.get("judge_rationale"),
            judge_retried=cached.get("judge_retried", False),
            query_used=cached.get("query_used"),
        )
