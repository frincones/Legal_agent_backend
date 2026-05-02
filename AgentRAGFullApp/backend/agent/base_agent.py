"""Base agent: orchestrates retrieval pipeline + LLM response + conversation memory."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import AsyncGenerator, Dict, List, Optional, Tuple

from config.schema import AppConfig
from ingestion.conversation_memory import ConversationMemory
from ingestion.embedder import create_embedder
from retrieval.pipeline import RetrievalPipeline
from storage.base import BaseStorage
from agent.system_prompts import build_system_prompt
from agent.response_builder import (
    build_context,
    build_legal_context,
    get_sources,
    get_confidence,
    format_response_with_sources,
    sanitize_llm_response,
    format_live_source_results,
    format_vigencia_results,
)
from utils.llm import get_openai_client

logger = logging.getLogger(__name__)


class RAGAgent:
    """
    Stateless RAG agent that:
    1. Loads conversation history from storage per session
    2. Routes query through retrieval pipeline (RAG-first)
    3. Builds context from retrieved data
    4. Generates response using primary LLM
    5. Persists exchange back to conversation memory
    """

    def __init__(self, config: AppConfig, storage: BaseStorage):
        self.config = config
        self.storage = storage
        self.retrieval = RetrievalPipeline(config, storage)
        self.embedder = create_embedder(
            model=config.ingestion.embedding.model,
            use_cache=True,
        )
        self.memory = ConversationMemory(
            storage=storage,
            embedder=self.embedder,
            utility_model=config.agent.utility_model,
        )
        # TTL cache for the allow-list of loaded document titles.
        # Refreshed every 5 min or when invalidated manually after ingestion.
        self._allow_list_cache: Optional[List[str]] = None
        self._allow_list_cached_at: float = 0.0
        self._allow_list_ttl_s: float = 30.0  # 30 seconds — fast refresh after ingest

    async def _build_messages(
        self,
        message: str,
        session_id: str,
        history_limit: int = 10,
    ) -> tuple[List[Dict[str, str]], dict]:
        """Build the message list for the LLM, including system prompt and history.

        The retrieval query is enriched with conversation history so that short
        follow-up messages like "y si yo me robé una computadora" carry the
        context of the previous exchange (in this case, "despido"). Without this,
        the RAG would search only for "robo" and miss the laboral chunks.
        """

        # Step 1: Load conversation history FIRST (we need it for query expansion)
        history: list = []
        try:
            history = await self.storage.get_session_messages(session_id)
        except Exception as e:
            logger.debug("Could not load history for session %s: %s", session_id, e)

        # Step 2: Build a "contextualized query" for retrieval that combines
        # recent conversation context with the new user message. This helps
        # the RAG find relevant chunks even on short follow-up turns.
        retrieval_query = self._build_retrieval_query(message, history)

        # Step 3: Retrieve context using the enriched query
        retrieval_result = await self.retrieval.retrieve(
            query=retrieval_query,
            session_id=session_id,
        )

        # Step 3.5: Legal mode — if RAG didn't find good results,
        # ask LLM what norm is needed and auto-ingest it
        llm_ingested = []
        if self._is_legal_mode():
            has_good = retrieval_result.results and any(
                r.best_score > 0.5 for r in retrieval_result.results[:3]
            )
            if not has_good:
                llm_ingested, _ = await self._identify_needed_norms_via_llm(message, retrieval_result)
                if llm_ingested:
                    self.invalidate_doc_cache()
                    # Re-run retrieval with new docs
                    retrieval_result = await self.retrieval.retrieve(
                        query=retrieval_query, session_id=session_id,
                    )
                    logger.info(f"Re-retrieval after LLM-identified ingest: {llm_ingested}")

        # Always preserve the user's actual message for display/logging purposes
        retrieval_result.query.original_query = message

        sources = get_sources(retrieval_result)
        confidence = get_confidence(retrieval_result)
        intent = (
            retrieval_result.query.intent.value
            if retrieval_result.query.intent
            else "knowledge"
        )

        # Legal mode: search live sources, auto-ingest missing norms, verify vigencia
        live_results = None
        vigencia_results = None
        is_legal = self._is_legal_mode()
        did_ingest = False

        if is_legal and intent != "conversation":
            # Phase 1: Search live sources + check vigencia
            live_results, vigencia_results = await self._enrich_with_legal_sources(
                retrieval_query, retrieval_result
            )

            # Phase 2: Auto-ingest any norms found in live sources that aren't in RAG
            if live_results:
                did_ingest = await self._auto_ingest_live_results(live_results)

            # Phase 3: If we ingested something, RE-RUN retrieval to pick up new chunks
            if did_ingest:
                self.invalidate_doc_cache()
                retrieval_result = await self.retrieval.retrieve(
                    query=retrieval_query,
                    session_id=session_id,
                )
                retrieval_result.query.original_query = message
                sources = get_sources(retrieval_result)
                confidence = get_confidence(retrieval_result)
                # Re-check vigencia with new retrieval results
                _, vigencia_results = await self._enrich_with_legal_sources(
                    retrieval_query, retrieval_result
                )
                logger.info("Re-ran retrieval after auto-ingest, new sources: %s", sources)

        # Build context: legal mode uses enriched context with live sources + vigencia
        if is_legal and (live_results or vigencia_results):
            context = build_legal_context(retrieval_result, live_results, vigencia_results)
            # Add live source names to the sources list
            if live_results:
                for lr in live_results:
                    name = f"{getattr(lr, 'titulo', '')} ({getattr(lr, 'source', '')})"
                    if name.strip(" ()"):
                        sources.append(name)
        else:
            context = build_context(retrieval_result)

        # Load the full list of documents in the corpus for the allow-list.
        # Cached with 5-min TTL: avoids hitting Postgres on every chat turn.
        loaded_doc_titles = await self._get_loaded_doc_titles()

        system_prompt = build_system_prompt(
            agent_name=self.config.agent.name,
            agent_role=self.config.agent.role,
            context=context,
            intent=intent,
            sources=sources,
            confidence=confidence,
            was_refined=retrieval_result.was_refined,
            refined_query=retrieval_result.refined_query,
            custom_template=self.config.agent.system_prompt_template,
            loaded_documents=loaded_doc_titles,
        )

        messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]

        # Step 4: Append recent history so the LLM has full conversation context
        if history:
            tail = history[-(history_limit * 2):]
            for h in tail:
                messages.append({"role": h["role"], "content": h["content"]})

        messages.append({"role": "user", "content": message})

        meta = {
            "intent": intent,
            "sources": sources,
            "context": context,
            "retrieval_result": retrieval_result,
            "retrieval_query": retrieval_query,
            "loaded_documents": loaded_doc_titles,
            "vigencia_results": vigencia_results,
            "live_results": live_results,
            "llm_ingested": llm_ingested,
        }
        return messages, meta

    def _build_retrieval_query(self, message: str, history: list) -> str:
        """Combine recent conversation context with the new message.

        This is critical for follow-up turns. Example:
        - Previous: "me quieren despedir y cumplo mis funciones"
        - New: "pero me robé una computadora"
        - Combined: the second message is enriched with "despido / trabajo /
          empleador" context so vector search finds laboral chunks instead
          of treating "robo" as a pure penal term.
        """
        if not history:
            return message

        # Take the last 2-3 user messages (most recent context wins)
        recent_user_msgs = [
            h["content"] for h in history[-6:]
            if h.get("role") == "user"
        ]

        # If no prior user messages, just use the new one
        if not recent_user_msgs:
            return message

        # Concatenate previous user messages with the new one for retrieval.
        # We use the user side only (not the assistant) because:
        # 1. The user's words contain the situation
        # 2. The assistant's reply is verbose and would dilute embeddings
        previous_context = " ".join(recent_user_msgs[-2:])  # last 2 user turns
        combined = f"{previous_context} {message}"

        # Cap at ~500 chars to keep embedding tokens reasonable
        if len(combined) > 500:
            combined = combined[-500:]

        return combined

    async def chat(self, message: str, session_id: Optional[str] = None) -> Dict:
        """Process a user message and return the agent's response."""
        from utils.usage_tracker import tracker

        session_id = session_id or str(uuid.uuid4())

        # Legal mode: auto-ingest missing norms BEFORE building messages
        ingested_norms = []
        if self._is_legal_mode():
            ingested_norms = await self._auto_ingest_missing_norms(message)
            if ingested_norms:
                self.invalidate_doc_cache()

        messages, meta = await self._build_messages(message, session_id)
        if ingested_norms:
            meta["ingested_norms"] = ingested_norms

        client = get_openai_client()
        response = await client.chat.completions.create(
            model=self.config.agent.primary_model,
            messages=messages,
            temperature=self.config.agent.temperature,
            max_tokens=self.config.agent.max_tokens,
        )

        if response.usage:
            tracker.record_chat(
                model=self.config.agent.primary_model,
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
                purpose="main_response",
                session_id=session_id,
            )

        assistant_message = response.choices[0].message.content.strip()
        assistant_message = sanitize_llm_response(
            assistant_message, allowed_documents=meta.get("loaded_documents")
        )

        # Append vigencia verification (legal mode)
        if meta.get("vigencia_results"):
            vigencia_lines = ["\n\n---\n**Vigencia Verificada:**"]
            for v in meta["vigencia_results"]:
                if v.estado == "VIGENTE":
                    vigencia_lines.append(f"- ✅ {v.tipo} {v.numero or ''} de {v.anio or ''} — VIGENTE")
                elif v.estado == "DEROGADA":
                    derog_info = ""
                    if v.derogaciones:
                        d = v.derogaciones[0]
                        derog_info = f" por {d.get('norma_tipo', '')} {d.get('norma_numero', '')} de {d.get('norma_anio', '')}"
                    vigencia_lines.append(f"- ❌ {v.tipo} {v.numero or ''} de {v.anio or ''} — DEROGADA{derog_info}")
                elif v.estado == "MODIFICADA":
                    vigencia_lines.append(f"- ⚠️ {v.tipo} {v.numero or ''} de {v.anio or ''} — MODIFICADA")
                elif not v.encontrada:
                    pass
                else:
                    vigencia_lines.append(f"- {v.tipo} {v.numero or ''} de {v.anio or ''} — {v.estado}")
            if len(vigencia_lines) > 1:
                assistant_message += "\n".join(vigencia_lines)

        assistant_message = format_response_with_sources(
            assistant_message, meta["retrieval_result"]
        )

        # ALWAYS persist chat history (lightweight) so the next message in
        # this session can see the previous turns.
        await self._save_chat_history(
            session_id=session_id,
            user_message=message,
            assistant_message=assistant_message,
            intent=meta["intent"],
            sources_used=meta["sources"],
        )

        # Optionally persist exchange to conversation memory (heavyweight, with
        # embeddings). Background task — does not block the response.
        self._schedule_memory_save(
            session_id=session_id,
            user_message=message,
            assistant_message=assistant_message,
            intent=meta["intent"],
            sources_used=meta["sources"],
        )

        return {
            "response": assistant_message,
            "intent": meta["intent"],
            "sources": meta["sources"],
            "session_id": session_id,
        }

    @staticmethod
    def _evt(etype: str, **kwargs) -> str:
        """Emit a NDJSON event line."""
        import json as _json
        return _json.dumps({"type": etype, **kwargs}, ensure_ascii=False) + "\n"

    async def chat_stream(
        self,
        message: str,
        session_id: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """Stream response as NDJSON (one JSON object per line).

        Event types:
          {"type":"status","text":"..."}       — thinking step
          {"type":"ingest","norm":"..."}       — downloading norm
          {"type":"token","text":"..."}        — LLM content token
          {"type":"vigencia","data":{...}}     — vigencia check
          {"type":"jurisprudencia","data":{...}} — sentencia found
          {"type":"sources","data":[...]}      — sources used THIS turn
          {"type":"sourcerefs","data":[...]}   — source cards with URLs
          {"type":"casestate","data":{...}}    — accumulated case state
          {"type":"done","duration":N}         — stream complete
        """
        from utils.usage_tracker import tracker

        session_id = session_id or str(uuid.uuid4())
        start_time = time.time()

        # Capture every NDJSON event for persistence into
        # conversations.activity_metadata. The wrapper is a drop-in for
        # self._evt so the existing 25+ `yield E(...)` call sites stay
        # untouched. Skip token events (would bloat the JSONB to MB on
        # long answers) — the assistant_message column already stores
        # the final text. All other events (status/ingest/vigencia/
        # jurisprudencia/sources/sourcerefs/clarify/case_shift/compact/
        # casestate/done) are captured with a millisecond timestamp.
        activity_events: list[dict] = []

        def E(etype: str, **kwargs) -> str:
            if etype != "token":
                activity_events.append({
                    "type": etype,
                    "ts": int((time.time() - start_time) * 1000),
                    **kwargs,
                })
            return self._evt(etype, **kwargs)

        yield E("status", text="Analizando consulta...")

        # Phase 0: Load Case State (accumulated context across all turns)
        from agent.case_state import CaseState
        case_state = CaseState(session_id)
        try:
            saved = await self.storage.get_case_state(session_id)
            if saved:
                case_state = CaseState(session_id, saved)
                logger.info(f"CaseState loaded: turn {case_state.turn_count}, {len(case_state.facts)} facts")
        except Exception as e:
            logger.debug("Could not load case state: %s", e)

        is_legal = self._is_legal_mode()

        # Inspect the most recent assistant turn ONCE so both Phase 0.5
        # (case-shift) and Phase 1.5 (clarify) can read it. Without this
        # the clarify gate kept re-firing after every form answer because
        # case_state.turn_count stays at 0 during clarify exchanges
        # (update_from_exchange runs only after a real LLM response).
        last_user_msg = ""
        previous_was_clarify = False
        if is_legal:
            try:
                prior = await self.storage.get_session_messages(session_id)
                for row in reversed(prior):
                    if row.get("role") == "user" and not last_user_msg:
                        last_user_msg = row.get("content", "")
                    if row.get("role") == "assistant":
                        if (row.get("content") or "").startswith("[CLARIFY]"):
                            previous_was_clarify = True
                        break  # last assistant turn analyzed
            except Exception as e:
                logger.debug("Could not inspect prior messages: %s", e)

        # Phase 0.5: Case-shift detection — if the user pivots to a
        # different case, archive the current case_state so its facts
        # don't bleed into the new investigation. The conversations table
        # (chat history) is left untouched. Skipped on first turn, on
        # continuation markers, on short messages, and when the previous
        # turn was a clarify (the user is just answering the form).
        is_shift, conf, reason = False, 0.0, ""
        if is_legal and case_state.turn_count > 0 and not previous_was_clarify:
            try:
                is_shift, conf, reason = await self._detect_case_shift(
                    message, case_state, last_user_msg
                )
            except Exception as e:
                logger.warning("Case-shift gate raised, defaulting to continuation: %s", e)
                is_shift, conf, reason = False, 0.0, ""

            if is_shift:
                snapshot = case_state.archive_current_case()
                if snapshot:
                    logger.info(
                        "Case shift detected (conf=%.2f): archived case #%d (%d facts), starting case #%d. Reason: %s",
                        conf, snapshot["index"], len(snapshot["facts"]),
                        case_state.case_index, reason[:80],
                    )
                    yield E(
                        "case_shift",
                        new_index=case_state.case_index,
                        archived_index=snapshot["index"],
                        confidence=round(conf, 2),
                        reason=reason or "Detecté un cambio de caso. Archivé el anterior y empiezo uno nuevo.",
                    )

        # Phase 1.5: Clarification gate — if the query is too vague to
        # answer accurately, ask 1-3 questions and stop. Cheap regex
        # filters short-circuit before any LLM call; failsafe is "no
        # clarify" so the rest of the pipeline always remains reachable.
        # CRITICAL: never re-fire clarify when the previous turn was
        # already a clarify ask. Without this guard, the user would get
        # endless rounds of questions (and even repeated ones) because
        # case_state.turn_count never increments during clarify exchanges,
        # so skip_on_followup never kicks in. One round per topic is the
        # contract — the next turn must run the real pipeline regardless.
        needs, clarify_questions, clarify_reason = False, [], ""
        if is_legal and not previous_was_clarify:
            try:
                needs, clarify_questions, clarify_reason = await self._needs_clarification(
                    message, case_state
                )
            except Exception as e:
                logger.warning("Clarification gate raised, defaulting to no-clarify: %s", e)
                needs, clarify_questions, clarify_reason = False, [], ""

            if needs:
                logger.info(
                    "Clarify triggered (%d questions): %s",
                    len(clarify_questions), clarify_reason[:80],
                )
                yield E(
                    "clarify",
                    questions=clarify_questions,
                    reason=clarify_reason or "Necesito un poco más de contexto para darte un dictamen preciso:",
                )
                # Persist the exchange so the chat history shows the request.
                # We tag the assistant message with [CLARIFY] for later reference.
                clarify_summary = (
                    "[CLARIFY] " + (clarify_reason or "Solicité información adicional")
                )
                try:
                    # Emit done first so it's part of the captured events.
                    pass
                except Exception:
                    pass

                # Emit done so the stream closes cleanly. No further phases run.
                yield E("done", duration=int(time.time() - start_time), clarified=True)

                # Now save with the captured events (clarify + done at minimum).
                turns_consumed_by_archived = sum(
                    (c.get("turn_count_at_archive", 0) for c in case_state.archived_cases), 0
                )
                turn_in_case = max(1, case_state.turn_count - turns_consumed_by_archived + 1)
                clarify_payload = {
                    "events": activity_events,
                    "case_index": case_state.case_index,
                    "turn_in_case": turn_in_case,
                    "duration_seconds": int(time.time() - start_time),
                    "clarified": True,
                }
                try:
                    await self._save_chat_history(
                        session_id=session_id,
                        user_message=message,
                        assistant_message=clarify_summary,
                        intent="clarify",
                        sources_used=[],
                        activity_metadata=clarify_payload,
                    )
                except Exception as e:
                    logger.debug("Could not save clarify chat history: %s", e)
                return

        # Phase 1: Auto-ingest norms mentioned by number in the query
        if is_legal:
            yield E("status", text="Revisando normas mencionadas...")
            ingested = await self._auto_ingest_missing_norms(message)
            if ingested:
                for n in ingested:
                    yield E("ingest", norm=f"{n}\n")
                self.invalidate_doc_cache()

        # Phase 2: Load history
        yield E("status", text="Cargando contexto de conversacion...")
        history: list = []
        try:
            history = await self.storage.get_session_messages(session_id)
        except Exception as e:
            logger.debug("Could not load history: %s", e)

        # Phase 2.5: Compact older history when it grows past the threshold.
        # The summary is persisted on case_state so the cost is paid once
        # per ~15-turn batch, not per turn. Failsafe: returns the original
        # history if anything fails.
        original_history_len = len(history)
        try:
            history = await self._compact_history_if_needed(history, case_state)
            if len(history) < original_history_len:
                yield E(
                    "compact",
                    summarized_messages=original_history_len - len(history) + 1,  # +1 for the synthetic summary
                    kept_recent=len(history) - 1,
                )
        except Exception as e:
            logger.warning("Compaction wrapper raised, keeping full history: %s", e)

        retrieval_query = self._build_retrieval_query(message, history)

        # Phase 3: Retrieval
        yield E("status", text="Buscando en documentos legales...")
        retrieval_result = await self.retrieval.retrieve(
            query=retrieval_query, session_id=session_id,
        )
        retrieval_result.query.original_query = message

        # Phase 4: Legal — ALWAYS try to identify and download needed norms
        llm_ingested = []
        llm_sentencias: list[dict] = []
        if is_legal:
            has_good = retrieval_result.results and any(
                r.best_score > 0.5 for r in retrieval_result.results[:3]
            )
            # ALWAYS investigate — even with some results, there might be
            # more specific norms needed for a complete answer
            yield E("status", text="Investigando normativa aplicable...")
            llm_ingested, llm_sentencias = await self._identify_needed_norms_via_llm(message, retrieval_result)
            if llm_ingested:
                for n in llm_ingested:
                    yield E("ingest", norm=f"{n}\n")
                self.invalidate_doc_cache()
                yield E("status", text="Buscando en documentos recien indexados...")
                retrieval_result = await self.retrieval.retrieve(
                    query=retrieval_query, session_id=session_id,
                )
                retrieval_result.query.original_query = message

        # Phase 5: Legal enrichment — live sources + vigencia
        live_results = None
        vigencia_results = None
        if is_legal:
            yield E("status", text="Consultando fuentes legales oficiales...")
            live_results, vigencia_results = await self._enrich_with_legal_sources(
                retrieval_query, retrieval_result
            )

            # Auto-ingest from live results if needed
            if live_results:
                did_ingest = await self._auto_ingest_live_results(live_results)
                if did_ingest:
                    self.invalidate_doc_cache()
                    yield E("status", text="Re-buscando con nuevas normas descargadas...")
                    retrieval_result = await self.retrieval.retrieve(
                        query=retrieval_query, session_id=session_id,
                    )
                    retrieval_result.query.original_query = message
                    _, vigencia_results = await self._enrich_with_legal_sources(
                        retrieval_query, retrieval_result
                    )

            if vigencia_results:
                yield E("status", text="Verificando vigencia de normas...")

            # Phase 5.5: Search + download + ingest jurisprudence
            yield E("status", text="Buscando jurisprudencia relevante...")
            juris_context = ""
            juris_for_frontend = []
            try:
                from api.legal import _source_router, _derogation_graph, _storage

                if _source_router:
                    juris_query = retrieval_query
                    if case_state.areas_involved:
                        juris_query = f"{' '.join(case_state.areas_involved)} {message}"

                    # Search sentencias in datos.gov.co + Corte Constitucional
                    juris_results = await _source_router.search_sentencias(
                        query=juris_query, limit=5,
                    )

                    if juris_results:
                        yield E("status", text=f"{len(juris_results)} sentencias encontradas, descargando...")

                        # Download full text and ingest each sentencia
                        juris_context = "\n\nJURISPRUDENCIA RELEVANTE:\n"
                        for jr in juris_results[:3]:
                            titulo = getattr(jr, 'titulo', '') or ''
                            source = getattr(jr, 'source', '') or ''
                            url = getattr(jr, 'url', '') or ''
                            preview = getattr(jr, 'preview', '') or ''
                            metadata = getattr(jr, 'metadata', {}) or {}

                            juris_context += f"\n--- {titulo} ({source}) ---\n"
                            if preview:
                                juris_context += f"{preview}\n"

                            # Try to download full sentencia text
                            if url and 'corte_cc' in source:
                                try:
                                    from legal_sources.corte_constitucional import CorteConstitucionalSource
                                    cc = CorteConstitucionalSource()
                                    # Parse tipo/numero/anio from titulo
                                    import re
                                    m = re.match(r'Sentencia\s+(\w+)-(\d+).*?(\d{4})', titulo)
                                    if m:
                                        sent_data = await cc.fetch_sentencia(m.group(1), int(m.group(2)), int(m.group(3)))
                                        if sent_data and sent_data.get('texto_completo'):
                                            texto = sent_data['texto_completo'][:3000]
                                            juris_context += f"Texto: {texto}\n"
                                            yield E("ingest", norm=f"Sentencia {titulo}\n")

                                            # Save to jurisprudencia table
                                            if _derogation_graph:
                                                from derogation.models import JurisprudenciaCreate, Corte, FuenteLegal
                                                jc = JurisprudenciaCreate(
                                                    corte=Corte.CORTE_CONSTITUCIONAL,
                                                    tipo_sentencia=m.group(1) if m else None,
                                                    numero=titulo,
                                                    fecha=None,
                                                    magistrado=metadata.get('magistrado'),
                                                    fuente_url=url,
                                                    fuente=FuenteLegal.CORTE_CC,
                                                    texto_completo=texto[:5000],
                                                    decision=preview[:500],
                                                )
                                                await _derogation_graph.insert_jurisprudencia(jc)

                                            # Ingest as RAG chunks
                                            if _storage:
                                                from ingestion.pipeline import IngestionPipeline
                                                from config.schema import load_config
                                                config = load_config()
                                                pipeline = IngestionPipeline(config, _storage)
                                                await pipeline.ingest_text(
                                                    text=texto,
                                                    title=f"Sentencia {titulo}",
                                                    source=url or source,
                                                    doc_type="jurisprudencia",
                                                )
                                    await cc.close()
                                except Exception as e:
                                    logger.debug(f"Sentencia download failed: {e}")

                            # Collect for frontend
                            juris_for_frontend.append({
                                "titulo": titulo, "source": source,
                                "url": url, "preview": preview[:200],
                                "magistrado": metadata.get('magistrado', ''),
                            })

                        # Re-run retrieval if we ingested sentencias
                        if any("[INGEST]" in str(j) for j in juris_for_frontend):
                            self.invalidate_doc_cache()
                            yield E("status", text="Re-buscando con jurisprudencia indexada...")
                            retrieval_result = await self.retrieval.retrieve(
                                query=retrieval_query, session_id=session_id,
                            )
                            retrieval_result.query.original_query = message

            except Exception as e:
                logger.warning(f"Jurisprudence pipeline failed: {e}")
                juris_context = ""
                juris_for_frontend = []

        # Phase 6: Build context & messages
        sources = get_sources(retrieval_result)
        confidence = get_confidence(retrieval_result)
        intent = retrieval_result.query.intent.value if retrieval_result.query.intent else "knowledge"

        if is_legal and (live_results or vigencia_results):
            context = build_legal_context(retrieval_result, live_results, vigencia_results)
            if live_results:
                for lr in live_results:
                    name = f"{getattr(lr, 'titulo', '')} ({getattr(lr, 'source', '')})"
                    if name.strip(" ()"):
                        sources.append(name)
        else:
            context = build_context(retrieval_result)

        # Append jurisprudencia context
        if juris_context:
            context += juris_context

        loaded_doc_titles = await self._get_loaded_doc_titles()

        # Inject CaseState into context (replaces need for 100 history messages)
        case_context = case_state.to_context_block()
        if case_context:
            context = case_context + "\n\n" + context

        # Phase 6.5: Live web verification. An orchestrator LLM calls
        # search_web / fetch_url tools to confirm vigencias, catch stale
        # RAG, surface recent jurisprudence, correct citation errors.
        # Runs BEFORE the system prompt is compiled so verified facts can
        # be injected into context. Degrades silently to previous behavior
        # when disabled or when no facts are found.
        web_cfg = getattr(self.config, "web_research", None)
        if web_cfg and web_cfg.enabled and is_legal and (
            not web_cfg.only_for_legal or is_legal
        ):
            try:
                from agent.web_research import run_web_research

                # Build a compact summary of what the agent already has.
                summary_parts = []
                if vigencia_results:
                    summary_parts.append("Vigencia local:")
                    for v in vigencia_results[:8]:
                        summary_parts.append(
                            f"  - {v.tipo} {v.numero}/{v.anio} = {v.estado}"
                        )
                if llm_ingested:
                    summary_parts.append(f"Normas/sentencias ingestadas: {', '.join(llm_ingested[:10])}")
                if llm_sentencias:
                    titles = [s.get("titulo", "") for s in llm_sentencias[:6]]
                    summary_parts.append(f"Sentencias conocidas: {', '.join(titles)}")
                if case_state.areas_involved:
                    summary_parts.append(f"Áreas: {', '.join(case_state.areas_involved)}")
                agent_summary = "\n".join(summary_parts) or "Sin datos previos."

                yield E("status", text="Verificando hechos en línea...")

                verified_facts = ""
                scrape_do_token = None
                try:
                    legal_cfg = getattr(self.config, "legal_sources", None)
                    if legal_cfg:
                        scrape_do_token = getattr(legal_cfg, "scrape_do_token", None)
                except Exception:
                    scrape_do_token = None

                async for evt in run_web_research(
                    user_question=message,
                    agent_context_summary=agent_summary,
                    model=web_cfg.model,
                    max_tool_calls=web_cfg.max_tool_calls,
                    session_id=session_id,
                    scrape_do_token=scrape_do_token,
                ):
                    etype = evt.get("type")
                    if etype == "web_search":
                        yield E("web_search", query=evt.get("query", ""))
                    elif etype == "web_result":
                        yield E("web_result",
                                count=evt.get("count", 0),
                                first_title=evt.get("first_title", ""))
                    elif etype == "web_fetch":
                        yield E("web_fetch", url=evt.get("url", ""))
                    elif etype == "web_fetch_done":
                        yield E("web_fetch_done",
                                url=evt.get("url", ""),
                                ok=evt.get("ok", False),
                                title=evt.get("title", ""))
                    elif etype == "web_research_done":
                        verified_facts = evt.get("facts", "")

                if verified_facts and not verified_facts.upper().startswith("SIN_HALLAZGOS"):
                    # Prepend verified facts so the main LLM reads them
                    # before the generic RAG context. Tag clearly so the
                    # system prompt can instruct the model to TRUST these
                    # over internal training.
                    context = (
                        "## HECHOS VERIFICADOS EN LÍNEA (PRIORITARIOS — usa estos sobre cualquier otro conocimiento)\n"
                        + verified_facts
                        + "\n\n---\n\n"
                        + context
                    )
                    logger.info(
                        "web_research injected %d chars of verified facts",
                        len(verified_facts),
                    )
            except Exception as e:
                logger.warning("web_research phase failed (non-fatal): %s", e)

        system_prompt = build_system_prompt(
            agent_name=self.config.agent.name,
            agent_role=self.config.agent.role,
            context=context, intent=intent, sources=sources,
            confidence=confidence,
            was_refined=retrieval_result.was_refined,
            refined_query=retrieval_result.refined_query,
            custom_template=self.config.agent.system_prompt_template,
            loaded_documents=loaded_doc_titles,
        )

        llm_messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
        # With CaseState, we only need last 5 turns for immediate context
        if history:
            for h in history[-(5 * 2):]:
                llm_messages.append({"role": h["role"], "content": h["content"]})
        llm_messages.append({"role": "user", "content": message})

        # Phase 7: Stream LLM response
        yield E("status", text="Generando respuesta fundamentada...")

        client = get_openai_client()
        full_response = ""
        first_token = True

        stream = await client.chat.completions.create(
            model=self.config.agent.primary_model,
            messages=llm_messages,
            temperature=self.config.agent.temperature,
            max_tokens=self.config.agent.max_tokens,
            stream=True,
            stream_options={"include_usage": True},
        )

        async for chunk in stream:
            if chunk.usage:
                tracker.record_chat(
                    model=self.config.agent.primary_model,
                    input_tokens=chunk.usage.prompt_tokens,
                    output_tokens=chunk.usage.completion_tokens,
                    purpose="main_response_stream",
                    session_id=session_id,
                )
                continue
            if chunk.choices and chunk.choices[0].delta.content:
                text = chunk.choices[0].delta.content
                yield E("token", text=text)
                full_response += text

        # Phase 8: Post-response metadata
        full_response = sanitize_llm_response(full_response, allowed_documents=loaded_doc_titles)

        # Vigencia: check ALL norms mentioned in response + ingested this turn
        import re as _re
        all_vigencia = list(vigencia_results or [])
        # Only norms we ALREADY found count as "checked" — a Phase 5 check that
        # came back encontrada=False (graph miss without live fallback) must be
        # retried here with check_live_sources=True so the post-response
        # badges still fire for norms the answer quotes but we never ingested.
        checked_keys = set()
        if vigencia_results:
            for v in vigencia_results:
                if v.encontrada:
                    checked_keys.add(f"{v.tipo}:{v.numero}:{v.anio}")
        norm_patterns = [
            (r"[Ll]ey\s+(\d+)\s+(?:de\s+)?(\d{4})", "LEY"),
            (r"[Dd]ecreto\s+(\d+)\s+(?:de\s+)?(\d{4})", "DECRETO"),
            (r"[Rr]esoluci[oó]n\s+(\d+)\s+(?:de\s+)?(\d{4})", "RESOLUCION"),
        ]
        for pattern, tipo in norm_patterns:
            for match in _re.finditer(pattern, full_response):
                key = f"{tipo}:{match.group(1)}:{match.group(2)}"
                if key not in checked_keys:
                    checked_keys.add(key)
                    try:
                        from api.legal import _vigencia_checker
                        if _vigencia_checker:
                            # check_live_sources=True falls back to datos.gov.co
                            # SUIN when the local derogation graph has no entry.
                            # Needed to detect norms the answer mentions but we
                            # never ingested (e.g. a derogated law cited for
                            # context — the old check returned encontrada=False
                            # and the vigencia badge never reached the user).
                            v = await _vigencia_checker.check(
                                tipo, int(match.group(1)), int(match.group(2)),
                                check_live_sources=True,
                            )
                            if v.encontrada:
                                all_vigencia.append(v)
                    except Exception:
                        pass

        # Emit vigencia events
        for v in all_vigencia:
            if v.encontrada:
                vdata = {"tipo": v.tipo, "numero": v.numero, "anio": v.anio,
                         "estado": v.estado, "titulo": v.titulo}
                if v.derogaciones:
                    d = v.derogaciones[0]
                    vdata["derogada_por"] = f"{d.get('norma_tipo','')} {d.get('norma_numero','')} de {d.get('norma_anio','')}"
                yield E("vigencia", data=vdata)

        # Jurisprudencia: merge three sources
        #   1. juris_for_frontend — from search_sentencias (Phase 5.5, rare: only
        #      fires when the user writes an explicit sentencia ID).
        #   2. llm_sentencias      — from _identify_needed_norms_via_llm (topical;
        #      LLM proposes relevant sentencias and we fetch/ingest them).
        #   3. full_response match — sentencias the final answer cited but we
        #      hadn't tracked yet.
        import json
        all_juris = list(juris_for_frontend) if 'juris_for_frontend' in dir() else []
        for js in llm_sentencias:
            if not any(j.get("titulo") == js.get("titulo") for j in all_juris):
                all_juris.append(js)

        # Extract sentencia citations from the LLM response. Accept both the
        # "Sentencia T-388 de 2019" long form and the compact "Sentencia
        # T-388/19" / "Sentencia T-388/2019" form with 2- or 4-digit year.
        sentencia_pattern = r"[Ss]entencia\s+(SU|T|C|A)[-\s]?(\d+)[\s/-]+(?:de\s+)?(\d{2,4})"
        for match in _re.finditer(sentencia_pattern, full_response):
            tipo_s = match.group(1).upper()
            num_s = match.group(2)
            anio_raw = match.group(3)
            if len(anio_raw) == 2:
                anio_s = f"20{anio_raw}" if int(anio_raw) < 50 else f"19{anio_raw}"
            else:
                anio_s = anio_raw
            ref = {"titulo": f"Sentencia {tipo_s}-{num_s} de {anio_s}",
                   "source": "citada en respuesta", "url": "",
                   "preview": f"Corte Constitucional - {tipo_s}-{num_s}/{anio_s}"}
            if not any(j.get("titulo") == ref["titulo"] for j in all_juris):
                all_juris.append(ref)

        for jf in all_juris:
            yield E("jurisprudencia", data=jf)

        # Sources with URLs for sidebar
        if sources:
            yield E("sources", data=sources)

        # Emit source references with real URLs for the activity panel
        source_refs = []
        seen_titles = set()

        def _add_ref(url, title, source, preview):
            if title and title not in seen_titles and title != 'NULL':
                seen_titles.add(title)
                source_refs.append({"url": url or '', "title": title, "source": source, "preview": preview[:120]})

        # From live search results
        if live_results:
            for lr in live_results:
                titulo = getattr(lr, 'titulo', '') or ''
                url = getattr(lr, 'url', '') or ''
                src = getattr(lr, 'source', '') or ''
                preview = getattr(lr, 'preview', '') or ''
                # Build URL for datos_gov results that have no URL
                if not url and getattr(lr, 'numero', None) and getattr(lr, 'anio', None):
                    tipo_l = (getattr(lr, 'tipo', '') or '').lower()
                    num = getattr(lr, 'numero', '')
                    anio = getattr(lr, 'anio', '')
                    url = f"http://www.secretariasenado.gov.co/senado/basedoc/{tipo_l}_{num}_{anio}.html"
                _add_ref(url, titulo or f"{getattr(lr,'tipo','')} {getattr(lr,'numero','')} de {getattr(lr,'anio','')}", src, preview)

        # From norms cited in response (extract and build URLs)
        for match in _re.finditer(r"[Ll]ey\s+(\d+)\s+(?:de\s+)?(\d{4})", full_response):
            _add_ref(f"http://www.secretariasenado.gov.co/senado/basedoc/ley_{match.group(1)}_{match.group(2)}.html",
                     f"Ley {match.group(1)} de {match.group(2)}", "secretariasenado.gov.co", "Texto oficial Senado")
        for match in _re.finditer(r"[Dd]ecreto\s+(\d+)\s+(?:de\s+)?(\d{4})", full_response):
            _add_ref(f"http://www.secretariasenado.gov.co/senado/basedoc/decreto_{match.group(1)}_{match.group(2)}.html",
                     f"Decreto {match.group(1)} de {match.group(2)}", "secretariasenado.gov.co", "Texto oficial Senado")

        # From loaded RAG documents
        for s in sources:
            if 'datos_gov' in s or 'NULL' in s:
                continue  # Skip generic datos.gov entries
            if 'senado' in s.lower() or 'codigo' in s.lower() or 'ley_' in s.lower():
                _add_ref("http://www.secretariasenado.gov.co/senado/basedoc/", s, "secretariasenado.gov.co", "Texto oficial Senado")
            elif 'funcionpublica' in s.lower():
                ref = {"url": "https://www.funcionpublica.gov.co/eva/gestornormativo/",
                       "title": s, "source": "funcionpublica.gov.co", "preview": "Gestor Normativo de Funcion Publica"}
                if not any(r['title'] == ref['title'] for r in source_refs):
                    source_refs.append(ref)

        if source_refs:
            yield E("sourcerefs", data=source_refs)

        # Update CaseState with this exchange
        try:
            await case_state.update_from_exchange(message, full_response, self.config.agent.utility_model)
            await self.storage.save_case_state(session_id, case_state.to_dict(), case_state.turn_count)
            yield E("casestate", data=case_state.to_dict())
        except Exception as e:
            logger.warning(f"CaseState save failed: {e}")

        # Duration
        duration = int(time.time() - start_time)
        yield E("done", duration=duration)

        # Build the activity metadata payload that the frontend will use
        # to rebuild this turn's Activity Panel and inline markers when
        # the session is reopened. case_index/turn_in_case let the UI
        # group panels by case across multi-case sessions.
        turns_consumed_by_archived = sum(
            (c.get("turn_count_at_archive", 0) for c in case_state.archived_cases), 0
        )
        turn_in_case = max(1, case_state.turn_count - turns_consumed_by_archived)
        activity_payload = {
            "events": activity_events,
            "case_index": case_state.case_index,
            "turn_in_case": turn_in_case,
            "duration_seconds": duration,
        }

        # ALWAYS persist chat history (lightweight) for in-session context
        await self._save_chat_history(
            session_id=session_id,
            user_message=message,
            assistant_message=full_response,
            intent=intent,
            sources_used=sources,
            activity_metadata=activity_payload,
        )

        # Optional: heavyweight RAG memory save (background, non-blocking)
        self._schedule_memory_save(
            session_id=session_id,
            user_message=message,
            assistant_message=full_response,
            intent=intent,
            sources_used=sources,
        )

    async def _build_chitchat_messages(
        self,
        message: str,
        session_id: str,
        history_limit: int = 6,
    ) -> List[Dict[str, str]]:
        """Build a minimal message list for chitchat turns (no retrieval).

        Used by chat_chitchat / chat_chitchat_stream to short-circuit the RAG
        pipeline for trivial turns like greetings, thanks, and farewells.
        Skips: embedding, hybrid_search, cross-encoder rerank, sources footer.
        Result: time-to-first-token drops from ~3s to ~0.5s.
        """
        history: list = []
        try:
            history = await self.storage.get_session_messages(session_id)
        except Exception as e:
            logger.debug("Could not load history for session %s: %s", session_id, e)

        system_prompt = (
            f"Eres {self.config.agent.name}, {self.config.agent.role}. "
            "El usuario te ha enviado un saludo, agradecimiento o mensaje conversacional "
            "breve que no requiere consultar documentos. Responde de forma cordial, "
            "concisa (1-2 frases) y en el mismo idioma del usuario. No inventes citas "
            "legales. Si el usuario quiere una consulta real, invitalo a formularla."
        )

        messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]

        if history:
            tail = history[-(history_limit * 2):]
            for h in tail:
                messages.append({"role": h["role"], "content": h["content"]})

        messages.append({"role": "user", "content": message})
        return messages

    async def chat_chitchat(
        self,
        message: str,
        session_id: Optional[str] = None,
    ) -> Dict:
        """Lightweight chat path for greetings / trivial turns. No RAG."""
        from utils.usage_tracker import tracker

        session_id = session_id or str(uuid.uuid4())
        messages = await self._build_chitchat_messages(message, session_id)

        client = get_openai_client()
        response = await client.chat.completions.create(
            model=self.config.agent.primary_model,
            messages=messages,
            temperature=0.3,
            max_tokens=200,
        )

        if response.usage:
            tracker.record_chat(
                model=self.config.agent.primary_model,
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
                purpose="chitchat",
                session_id=session_id,
            )

        assistant_message = (response.choices[0].message.content or "").strip()

        await self._save_chat_history(
            session_id=session_id,
            user_message=message,
            assistant_message=assistant_message,
            intent="conversation",
            sources_used=[],
        )

        return {
            "response": assistant_message,
            "intent": "conversation",
            "sources": [],
            "session_id": session_id,
        }

    async def chat_chitchat_stream(
        self,
        message: str,
        session_id: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """Streaming variant of chat_chitchat. No RAG pipeline."""
        from utils.usage_tracker import tracker
        E = self._evt

        session_id = session_id or str(uuid.uuid4())
        messages = await self._build_chitchat_messages(message, session_id)

        client = get_openai_client()
        full_response = ""
        stream = await client.chat.completions.create(
            model=self.config.agent.primary_model,
            messages=messages,
            temperature=0.3,
            max_tokens=200,
            stream=True,
            stream_options={"include_usage": True},
        )

        async for chunk in stream:
            if chunk.usage:
                tracker.record_chat(
                    model=self.config.agent.primary_model,
                    input_tokens=chunk.usage.prompt_tokens,
                    output_tokens=chunk.usage.completion_tokens,
                    purpose="chitchat_stream",
                    session_id=session_id,
                )
                continue

            if chunk.choices and chunk.choices[0].delta.content:
                text = chunk.choices[0].delta.content
                full_response += text
                yield E("token", text=text)

        await self._save_chat_history(
            session_id=session_id,
            user_message=message,
            assistant_message=full_response,
            intent="conversation",
            sources_used=[],
        )

    async def _save_chat_history(
        self,
        session_id: str,
        user_message: str,
        assistant_message: str,
        intent: str,
        sources_used: list,
        activity_metadata: Optional[dict] = None,
    ) -> None:
        """Lightweight chat history save (no embeddings, no LLM calls).

        Always runs so the next user turn in the same session can see the
        previous exchange via get_session_messages(). Independent from the
        heavier RAG memory (save_to_rag) which embeds and re-ranks past
        conversations as additional retrieval context.

        ``activity_metadata`` is optional; when provided, the per-turn
        timeline is persisted on the conversation row so the Activity
        Panel can be reconstructed verbatim on session reload.
        """
        try:
            await self.storage.save_chat_message(
                session_id=session_id,
                user_message=user_message,
                assistant_message=assistant_message,
                intent=intent,
                sources_used=sources_used,
                activity_metadata=activity_metadata,
            )
        except Exception as e:
            logger.warning("Chat history save failed (non-fatal): %s", e)

    def _schedule_memory_save(
        self,
        session_id: str,
        user_message: str,
        assistant_message: str,
        intent: str,
        sources_used: list,
    ) -> None:
        """Schedule a non-blocking RAG conversation memory save.

        This is for SEMANTIC search of past conversations (heavyweight: embedding
        + relevance grading + summary). Skipped entirely if
        conversation_memory.save_to_rag is disabled.

        Note: chat history persistence (for in-session context) is handled by
        _save_chat_history and runs unconditionally.
        """
        if not self.config.retrieval.conversation_memory.save_to_rag:
            return

        async def _do_save():
            try:
                await self.memory.process_exchange(
                    session_id=session_id,
                    user_message=user_message,
                    assistant_message=assistant_message,
                    intent=intent,
                    sources_used=sources_used,
                )
            except Exception as e:
                logger.debug("Background conversation memory save failed: %s", e)

        try:
            asyncio.create_task(_do_save())
        except RuntimeError:
            # No running loop — skip silently
            pass

    async def _get_loaded_doc_titles(self) -> List[str]:
        """Return the allow-list of loaded document titles, cached for 5 min.

        The allow-list drives the anti-hallucination layer in the system prompt.
        Reading it from Postgres on every turn cost ~100-200ms per query; this
        cache brings it down to <1ms on the hot path. Cache is invalidated
        automatically on TTL expiry and manually via `invalidate_doc_cache()`
        after document ingestion or deletion.
        """
        now = time.monotonic()
        if (
            self._allow_list_cache is not None
            and (now - self._allow_list_cached_at) < self._allow_list_ttl_s
        ):
            return self._allow_list_cache

        try:
            all_docs_meta = await self.storage.list_documents()
            titles = [
                d["title"] for d in all_docs_meta
                if d.get("status") in (None, "completed")
                and "tmp" not in (d.get("title") or "").lower()
            ]
        except Exception as e:
            logger.debug("Could not load document allow-list: %s", e)
            titles = self._allow_list_cache or []

        self._allow_list_cache = titles
        self._allow_list_cached_at = now
        return titles

    def invalidate_doc_cache(self) -> None:
        """Force the next query to reload the document allow-list from DB.

        Call this after ingesting or deleting a document so that the prompt
        reflects the current corpus on the next user turn.
        """
        self._allow_list_cache = None
        self._allow_list_cached_at = 0.0

    async def _auto_ingest_missing_norms(self, message: str) -> list[str]:
        """Detect norm references in the query and auto-ingest any that aren't in the RAG.

        Returns list of norm names that were ingested (empty if none needed).
        """
        import re

        try:
            from api.legal import _source_router, _derogation_graph, _storage, _embedder
        except ImportError:
            return []

        if not _source_router or not _derogation_graph or not _storage:
            return []

        # Extract norm references from the user message
        patterns = [
            (r"(?:[Ll]ey)\s+(\d+)\s+(?:de\s+)?(\d{4})", "LEY"),
            (r"(?:[Dd]ecreto)\s+(\d+)\s+(?:de\s+)?(\d{4})", "DECRETO"),
            (r"(?:[Rr]esoluci[oó]n)\s+(\d+)\s+(?:de\s+)?(\d{4})", "RESOLUCION"),
        ]

        norm_refs = []
        seen = set()
        for pattern, tipo in patterns:
            for match in re.finditer(pattern, message):
                key = f"{tipo}:{match.group(1)}:{match.group(2)}"
                if key not in seen:
                    seen.add(key)
                    norm_refs.append({
                        "tipo": tipo,
                        "numero": int(match.group(1)),
                        "anio": int(match.group(2)),
                    })

        if not norm_refs:
            return []

        # Check which norms are already in the RAG (by document title)
        loaded_titles = await self._get_loaded_doc_titles()
        loaded_lower = {t.lower() for t in loaded_titles}

        ingested = []

        for ref in norm_refs:
            nombre = f"{ref['tipo']} {ref['numero']} de {ref['anio']}"
            nombre_variants = [
                nombre.lower(),
                f"{ref['tipo'].lower()} {ref['numero']} de {ref['anio']}",
                f"{ref['tipo'].lower()}_{ref['numero']}_{ref['anio']}",
                f"ley_{ref['numero']}_{ref['anio']}",
                f"resolucion_{ref['numero']}_{ref['anio']}",
                f"decreto_{ref['numero']}_{ref['anio']}",
            ]

            # Check if any variant is already loaded
            already_loaded = any(
                any(variant in title for variant in nombre_variants)
                for title in loaded_lower
            )

            if already_loaded:
                logger.info(f"Norm already in RAG: {nombre}")
                continue

            # Not loaded — fetch and ingest
            logger.info(f"Auto-ingesting missing norm: {nombre}")

            try:
                norm_data = await _source_router.fetch_norm(
                    ref["tipo"], ref["numero"], ref["anio"]
                )

                if not norm_data or not norm_data.get("texto_completo"):
                    logger.warning(f"Could not fetch norm: {nombre}")
                    continue

                # Ingest to graph
                from derogation.models import NormaCreate, TipoNorma, FuenteLegal
                norma_create = NormaCreate(
                    tipo=TipoNorma(ref["tipo"]),
                    numero=ref["numero"],
                    anio=ref["anio"],
                    titulo=norm_data.get("titulo", nombre),
                    fuente=FuenteLegal(norm_data.get("fuente", "manual")),
                    fuente_url=norm_data.get("fuente_url"),
                    fuente_id=norm_data.get("fuente_id"),
                    texto_completo=norm_data.get("texto_completo"),
                    sector=norm_data.get("sector"),
                    metadata=norm_data.get("metadata", {}),
                )

                if _embedder:
                    embed_text = f"{norm_data.get('titulo', '')} {norm_data.get('texto_completo', '')[:1000]}"
                    embeddings = await _embedder.generate_embeddings_batch([embed_text])
                    embedding = embeddings[0] if embeddings else None
                else:
                    embedding = None

                await _derogation_graph.insert_norma(norma_create, embedding=embedding)

                # Detect and register derogations
                from derogation.detector import detect_derogations
                from derogation.models import DerogacionCreate
                derogations = detect_derogations(norm_data.get("texto_completo", ""))
                for det in derogations:
                    if det.norma_afectada_numero and det.norma_afectada_anio:
                        affected = await _derogation_graph.get_norma(
                            det.norma_afectada_tipo or ref["tipo"],
                            det.norma_afectada_numero,
                            det.norma_afectada_anio,
                        )
                        if affected:
                            derog = DerogacionCreate(
                                norma_origen_id=str((await _derogation_graph.get_norma(ref["tipo"], ref["numero"], ref["anio"]))["id"]),
                                norma_destino_id=str(affected["id"]),
                                tipo=det.tipo_derogacion,
                                articulos_afectados=det.articulos_afectados,
                                fuente_texto=det.texto_fuente,
                                detectado_por="auto_regex",
                                confianza=det.confianza,
                            )
                            await _derogation_graph.insert_derogacion(derog)

                # Ingest to RAG (chunks)
                from ingestion.pipeline import IngestionPipeline
                from config.schema import load_config
                config = load_config()
                pipeline = IngestionPipeline(config, _storage)
                await pipeline.ingest_text(
                    text=norm_data.get("texto_completo", ""),
                    title=norm_data.get("titulo", nombre),
                    source=norm_data.get("fuente_url", "legal_source"),
                    doc_type="legal_norm",
                )

                ingested.append(nombre)
                logger.info(f"Auto-ingested: {nombre}")

            except Exception as e:
                logger.error(f"Auto-ingest failed for {nombre}: {e}")

        # Also check derogation chain: if a norm is derogated, auto-ingest the replacement
        for ref in norm_refs:
            try:
                vigencia = await self._check_derogation_and_ingest_replacement(ref, loaded_lower, ingested)
                if vigencia:
                    ingested.extend(vigencia)
            except Exception as e:
                logger.debug(f"Derogation chain check failed: {e}")

        return ingested

    async def _check_derogation_and_ingest_replacement(
        self, ref: dict, loaded_lower: set, already_ingested: list
    ) -> list[str]:
        """If a norm is derogated, auto-ingest the norm that replaced it."""
        from api.legal import _derogation_graph, _source_router, _storage, _embedder

        if not _derogation_graph:
            return []

        norma = await _derogation_graph.get_norma(ref["tipo"], ref["numero"], ref["anio"])
        if not norma or norma.get("estado") != "DEROGADA":
            return []

        # Find what derogated it
        derogations = await _derogation_graph.get_derogations_for(str(norma["id"]))
        ingested = []

        for d in derogations:
            replacement_nombre = f"{d.get('origen_tipo', '')} {d.get('origen_numero', '')} de {d.get('origen_anio', '')}"

            # Skip if already loaded or already ingested in this cycle
            if replacement_nombre in already_ingested:
                continue

            nombre_lower = replacement_nombre.lower()
            if any(nombre_lower in t for t in loaded_lower):
                continue

            # Fetch and ingest the replacement
            logger.info(f"Auto-ingesting replacement norm: {replacement_nombre}")
            try:
                norm_data = await _source_router.fetch_norm(
                    d.get("origen_tipo", ""), d.get("origen_numero", 0), d.get("origen_anio", 0)
                )
                if norm_data and norm_data.get("texto_completo"):
                    from ingestion.pipeline import IngestionPipeline
                    from config.schema import load_config
                    config = load_config()
                    pipeline = IngestionPipeline(config, _storage)
                    await pipeline.ingest_text(
                        text=norm_data.get("texto_completo", ""),
                        title=norm_data.get("titulo", replacement_nombre),
                        source=norm_data.get("fuente_url", "legal_source"),
                        doc_type="legal_norm",
                    )
                    ingested.append(replacement_nombre)
                    logger.info(f"Auto-ingested replacement: {replacement_nombre}")
            except Exception as e:
                logger.error(f"Auto-ingest replacement failed for {replacement_nombre}: {e}")

        return ingested

    # ─────────────────────────────────────────────────────────────────
    # Case-shift detection (Phase 0.5) and compaction (Phase 2.5)
    # ─────────────────────────────────────────────────────────────────
    # Words/phrases that strongly imply the user is continuing the same
    # case. When found at the start of the new message we skip the LLM
    # decisor — the answer is essentially given.
    _CONTINUATION_MARKERS_RE = re.compile(
        r"^\s*(y\b|pero\b|adem[aá]s\b|tambi[eé]n\b|respecto\b|sobre\s+(?:eso|esto|lo)\b|"
        r"siguiendo\b|continuando\b|en\s+ese\s+caso\b|entonces\b|"
        r"qu[eé]\s+(?:pasa|hacer|aplica)\s+con\b|y\s+si\b|"
        r"profundiza|am[ií]a|expl[ií]came\s+m[aá]s)",
        re.IGNORECASE,
    )

    async def _detect_case_shift(
        self,
        message: str,
        case_state,
        last_user_msg: str,
    ) -> Tuple[bool, float, str]:
        """Decide whether the new message starts a fundamentally different case.

        Returns (is_new_case, confidence, reason). Cheap pre-filters
        short-circuit the LLM call. Failsafe: any error returns (False, 0, "")
        and the pipeline continues with the existing case_state intact.
        """
        cfg = self.config.conversation_context
        if not cfg.case_shift_enabled:
            return False, 0.0, ""

        # First turn: nothing to shift away from.
        if case_state.turn_count == 0 or not (
            case_state.facts or case_state.norms_cited or case_state.conclusions
        ):
            return False, 0.0, ""

        # Continuation markers: user is clearly building on the previous turn.
        if self._CONTINUATION_MARKERS_RE.search(message[:80]):
            return False, 0.0, ""

        # Very short message → not enough signal to flip a case.
        if len(message.strip().split()) < 5:
            return False, 0.0, ""

        from agent.system_prompts import CASE_SHIFT_DETECTION_PROMPT
        from utils.usage_tracker import tracker

        # Compact view of the current case for the decisor's prompt.
        current_facts = case_state.facts[-5:] if case_state.facts else []
        current_areas = list(set(case_state.areas_involved))
        current_norms = list(set(case_state.norms_cited))[-5:]
        current_summary = (
            f"Área(s): {', '.join(current_areas) or 'no especificada'}.\n"
            f"Hechos clave: {' | '.join(current_facts) or 'sin hechos extraídos'}.\n"
            f"Normas analizadas: {', '.join(current_norms) or 'ninguna'}.\n"
            f"Turnos previos: {case_state.turn_count}."
        )

        try:
            client = get_openai_client()
            response = await client.chat.completions.create(
                model=cfg.case_shift_model,
                temperature=0,
                max_tokens=200,
                messages=[
                    {"role": "system", "content": CASE_SHIFT_DETECTION_PROMPT},
                    {"role": "user", "content": (
                        f"=== Caso actual ===\n{current_summary}\n\n"
                        f"=== Mensaje previo del usuario ===\n{(last_user_msg or '')[:400]}\n\n"
                        f"=== Mensaje NUEVO del usuario ===\n{message[:600]}"
                    )},
                ],
                response_format={"type": "json_object"},
            )

            if response.usage:
                tracker.record_chat(
                    model=cfg.case_shift_model,
                    input_tokens=response.usage.prompt_tokens,
                    output_tokens=response.usage.completion_tokens,
                    purpose="case_shift_detection",
                    session_id=case_state.session_id or "",
                )

            data = json.loads(response.choices[0].message.content or "{}")
        except Exception as e:
            logger.warning("Case-shift detector failed (defaulting to continuation): %s", e)
            return False, 0.0, ""

        decision = (data.get("decision") or "").upper()
        confidence = float(data.get("confidence") or 0.0)
        reason = (data.get("reason") or "").strip()

        if decision == "NEW_CASE" and confidence >= cfg.case_shift_min_confidence:
            return True, confidence, reason
        return False, confidence, reason

    async def _compact_history_if_needed(
        self,
        history: list,
        case_state,
    ) -> list:
        """Replace the older slice of history with an LLM-produced summary.

        Returns the rewritten history (or the original when no compaction
        happened). Persists the summary on `case_state` so subsequent turns
        don't re-pay the cost — the next call sees a short history and
        won't trigger compaction until enough new turns accumulate again.
        """
        cfg = self.config.conversation_context
        if not cfg.compaction_enabled or not history:
            return history

        if len(history) <= cfg.compaction_threshold_messages:
            # Below threshold — but if a summary already exists, splice it in
            # so the LLM doesn't lose the prior compacted narrative.
            if case_state.summary:
                summary_msg = {
                    "role": "assistant",
                    "content": f"[RESUMEN AUTOMÁTICO de turnos previos]\n{case_state.summary}",
                }
                return [summary_msg] + history
            return history

        keep = max(2, cfg.compaction_keep_recent)
        recent = history[-keep:]
        to_compact = history[:-keep]

        if len(to_compact) < cfg.compaction_min_to_summarize:
            # Not enough older messages to bother summarizing.
            if case_state.summary:
                summary_msg = {
                    "role": "assistant",
                    "content": f"[RESUMEN AUTOMÁTICO de turnos previos]\n{case_state.summary}",
                }
                return [summary_msg] + history
            return history

        from agent.system_prompts import COMPACT_SUMMARY_PROMPT
        from utils.usage_tracker import tracker

        # Serialize messages for the summarizer prompt.
        prior_summary = case_state.summary or "(sin resumen previo)"
        msgs_text_parts = []
        for m in to_compact:
            role = m.get("role", "user")
            content = (m.get("content") or "")[:1500]
            msgs_text_parts.append(f"[{role.upper()}] {content}")
        msgs_text = "\n\n".join(msgs_text_parts)[:18000]  # cap input size

        try:
            client = get_openai_client()
            response = await client.chat.completions.create(
                model=cfg.compaction_model,
                temperature=0.2,
                max_tokens=600,
                messages=[
                    {"role": "system", "content": COMPACT_SUMMARY_PROMPT},
                    {"role": "user", "content": (
                        f"=== Resumen previo ===\n{prior_summary}\n\n"
                        f"=== Mensajes a comprimir ({len(to_compact)} mensajes) ===\n{msgs_text}"
                    )},
                ],
            )

            new_summary = (response.choices[0].message.content or "").strip()
            if not new_summary:
                logger.warning("Compaction produced empty summary, keeping full history")
                return history

            if response.usage:
                tracker.record_chat(
                    model=cfg.compaction_model,
                    input_tokens=response.usage.prompt_tokens,
                    output_tokens=response.usage.completion_tokens,
                    purpose="history_compaction",
                    session_id=case_state.session_id or "",
                )

            # Persist on the case_state so future turns can reuse.
            case_state.summary = new_summary
            # Track up to which message we summarized; we use the last
            # element index because IDs are not always present in history rows.
            last = to_compact[-1] if to_compact else {}
            case_state.last_compacted_message_id = str(last.get("id") or last.get("timestamp") or "")

            logger.info(
                "Compacted %d messages into %d-char summary; recent kept: %d",
                len(to_compact), len(new_summary), len(recent),
            )

            summary_msg = {
                "role": "assistant",
                "content": f"[RESUMEN AUTOMÁTICO de {len(to_compact)} mensajes previos]\n{new_summary}",
            }
            return [summary_msg] + recent
        except Exception as e:
            logger.warning("Compaction LLM failed (keeping full history): %s", e)
            return history

    # ─────────────────────────────────────────────────────────────────
    # Clarification gate (Phase 1.5)
    # ─────────────────────────────────────────────────────────────────
    # Cheap regex pre-filters that skip the LLM call entirely. If any
    # matches, the query is considered "specific enough" — no clarify.
    _CLARIFY_NORM_REF_RE = re.compile(
        r"\b(ley|decreto|resoluci[oó]n|art[ií]culo|art\.|c[oó]digo|sentencia)\s+\d",
        re.IGNORECASE,
    )
    _CLARIFY_SPECIFIC_RE = re.compile(
        r"\b\d+\s*(meses|años|anos|millones|smlmv|d[ií]as|%|por\s*ciento|pcl)\b",
        re.IGNORECASE,
    )

    async def _needs_clarification(
        self, message: str, case_state
    ) -> Tuple[bool, List[Dict], str]:
        """Decide whether to pause and ask the user clarifying questions.

        Returns (needs, questions, reason). Cheap regex filters short-circuit
        the decision before any LLM call. Failsafe on every error path: if
        anything goes wrong, return (False, [], "") and let the normal
        pipeline run — clarification is opt-in quality, not load-bearing.
        """
        cfg = self.config.clarification
        if not cfg.enabled:
            return False, [], ""

        # Skip on follow-ups: if the user has already established context
        # in this session (case_state populated), they almost never want a
        # questionnaire — they want continuation.
        if cfg.skip_on_followup and case_state.turn_count > 0 and (
            case_state.facts or case_state.norms_cited
        ):
            return False, [], ""

        # Very short messages can't carry enough signal — let intent_router
        # route them (chitchat) or the main pipeline handle them.
        words = message.strip().split()
        if len(words) < 4:
            return False, [], ""

        # User cited a specific norm or supplied concrete figures → already
        # specific enough for an answer; no need to ask.
        if self._CLARIFY_NORM_REF_RE.search(message):
            return False, [], ""
        if self._CLARIFY_SPECIFIC_RE.search(message):
            return False, [], ""

        # All cheap filters passed → consult the LLM decisor.
        from agent.system_prompts import CLARIFY_DECISION_PROMPT
        from utils.usage_tracker import tracker

        try:
            client = get_openai_client()
            response = await client.chat.completions.create(
                model=cfg.model,
                temperature=0,
                max_tokens=400,
                messages=[
                    {"role": "system", "content": CLARIFY_DECISION_PROMPT},
                    {"role": "user", "content": message[:1000]},
                ],
                response_format={"type": "json_object"},
            )

            if response.usage:
                tracker.record_chat(
                    model=cfg.model,
                    input_tokens=response.usage.prompt_tokens,
                    output_tokens=response.usage.completion_tokens,
                    purpose="clarify_decision",
                    session_id=case_state.session_id or "",
                )

            raw = response.choices[0].message.content or "{}"
            data = json.loads(raw)
        except Exception as e:
            logger.warning(f"Clarification decisor failed (defaulting to no-clarify): {e}")
            return False, [], ""

        if data.get("status") != "NEEDS_CLARIFICATION":
            return False, [], data.get("reason", "")

        # Sanitize questions: enforce max_questions cap and required fields.
        raw_questions = data.get("questions") or []
        questions: List[Dict] = []
        for q in raw_questions[: cfg.max_questions]:
            if not isinstance(q, dict):
                continue
            label = (q.get("label") or "").strip()
            qid = (q.get("id") or "").strip()
            qtype = q.get("type") or "text"
            if not label or not qid:
                continue
            entry = {"id": qid, "label": label, "type": qtype}
            if qtype == "radio":
                opts = [str(o) for o in (q.get("options") or []) if o]
                if len(opts) < 2:
                    # Radio with <2 options is unusable — drop to text input.
                    entry["type"] = "text"
                else:
                    entry["options"] = opts
            questions.append(entry)

        if not questions:
            # LLM said clarify but produced no usable questions — fall back.
            return False, [], data.get("reason", "")

        return True, questions, data.get("reason", "")

    async def _identify_needed_norms_via_llm(self, message: str, retrieval_result):
        """Use LLM to identify which Colombian norms/sentencias are needed.

        Returns a tuple (ingested_labels, sentencias_frontend) where
        ingested_labels is a flat list of short names for the `ingest` UI
        event, and sentencias_frontend is a list of dicts ready to emit as
        `jurisprudencia` events (titulo, source, url, preview, magistrado).
        """
        import re

        try:
            # Build context about what's already loaded
            loaded = await self._get_loaded_doc_titles()
            loaded_str = ", ".join(loaded[:10]) if loaded else "ninguno"

            client = get_openai_client()
            response = await client.chat.completions.create(
                model=self.config.agent.utility_model,
                temperature=0,
                max_tokens=400,
                messages=[
                    {"role": "system", "content": (
                        "Eres un experto en derecho colombiano. El usuario hizo una consulta legal. "
                        "Identifica TODAS las normas colombianas (leyes, decretos, resoluciones, "
                        "codigos) que se necesitan para responder completamente esta consulta. "
                        "Incluye normas de CUALQUIER area: civil, penal, laboral, comercial, "
                        "administrativo, ambiental, deportivo, propiedad horizontal, etc. "
                        f"Ya estan cargados: {loaded_str}. "
                        "NO repitas normas ya cargadas. "
                        "Responde SOLO con formato: TIPO NUMERO AÑO (una por linea). "
                        "Para normas: LEY 675 2001\nDECRETO 1072 2015\n"
                        "Para sentencias de la Corte Constitucional: SENTENCIA T-388 2019\nSENTENCIA SU-049 2017\n"
                        "Incluye sentencias relevantes siempre que sea posible. "
                        "Si no se necesitan normas adicionales, responde: NONE"
                    )},
                    {"role": "user", "content": message},
                ],
            )

            answer = response.choices[0].message.content.strip()
            logger.info(f"LLM norm identification: {answer}")

            if "NONE" in answer.upper():
                return [], []

            # Parse response
            ingested: list[str] = []
            sentencias_frontend: list[dict] = []
            norm_pattern = r"(LEY|DECRETO|RESOLUCION|CODIGO)\s+(\d+)\s+(?:de\s+)?(\d{4})"
            # Sentencias arrive as "SENTENCIA T-388 2019" / "SENTENCIA SU-049 2017" /
            # "SENTENCIA C-1507 2000". The LLM prompt above asks for this shape.
            sent_pattern = r"SENTENCIA\s+(SU|T|C|A)-?(\d+)\s+(?:de\s+)?(\d{4})"

            for match in re.finditer(norm_pattern, answer, re.IGNORECASE):
                tipo = match.group(1).upper()
                numero = int(match.group(2))
                anio = int(match.group(3))
                nombre = f"{tipo} {numero} de {anio}"

                loaded = await self._get_loaded_doc_titles()
                already = any(str(numero) in t and str(anio) in t for t in [x.lower() for x in loaded])
                if already:
                    continue

                try:
                    from api.legal import _source_router, _storage
                    if not _source_router or not _storage:
                        continue

                    norm_data = await _source_router.fetch_norm(tipo, numero, anio)
                    if norm_data and norm_data.get("texto_completo"):
                        from ingestion.pipeline import IngestionPipeline
                        from config.schema import load_config
                        config = load_config()
                        pipeline = IngestionPipeline(config, _storage)
                        await pipeline.ingest_text(
                            text=norm_data["texto_completo"],
                            title=norm_data.get("titulo", nombre),
                            source=norm_data.get("fuente_url", "legal_source"),
                            doc_type="legal_norm",
                        )
                        ingested.append(nombre)
                        logger.info(f"LLM-identified norm ingested: {nombre}")
                except Exception as e:
                    logger.warning(f"Failed to ingest LLM-identified norm {nombre}: {e}")

            # Sentencias identified by the LLM: fetch via Corte Constitucional
            # relatoría and ingest into jurisprudencia + RAG chunks. Previously
            # ignored — the old parser only matched norms.
            for match in re.finditer(sent_pattern, answer, re.IGNORECASE):
                tipo_letra = match.group(1).upper()
                numero = int(match.group(2))
                anio = int(match.group(3))
                label = f"SENTENCIA {tipo_letra}-{numero} de {anio}"

                loaded = await self._get_loaded_doc_titles()
                already = any(
                    f"{tipo_letra.lower()}-{numero}" in t.lower() and str(anio)[-2:] in t
                    for t in loaded
                )
                if already:
                    # Already ingested in a previous turn — skip refetch but
                    # still surface it in the activity panel so the user sees
                    # the LLM considered it for this answer.
                    numero_str = f"{numero:03d}" if numero < 1000 else str(numero)
                    yy = str(anio)[-2:]
                    url = (
                        f"https://www.corteconstitucional.gov.co/relatoria/{anio}/"
                        f"SU{numero_str}-{yy}.htm"
                        if tipo_letra == "SU"
                        else f"https://www.corteconstitucional.gov.co/relatoria/{anio}/"
                             f"{tipo_letra}-{numero_str}-{yy}.htm"
                    )
                    sentencias_frontend.append({
                        "titulo": f"Sentencia {tipo_letra}-{numero}/{yy}",
                        "source": "rag_cache",
                        "url": url,
                        "preview": f"Corte Constitucional - {label} (ya indexada)",
                        "magistrado": "",
                    })
                    continue

                try:
                    from api.legal import _source_router, _storage, _derogation_graph
                    from legal_sources.corte_constitucional import CorteConstitucionalSource
                    cc = CorteConstitucionalSource()
                    try:
                        sent_data = await cc.fetch_sentencia(tipo_letra, numero, anio)
                    finally:
                        await cc.close()

                    if not (sent_data and sent_data.get("texto_completo")):
                        logger.info(f"LLM-identified sentencia not found at relatoría: {label}")
                        continue

                    texto = sent_data["texto_completo"]
                    titulo = sent_data.get("titulo") or label

                    if _storage:
                        from ingestion.pipeline import IngestionPipeline
                        from config.schema import load_config
                        config = load_config()
                        pipeline = IngestionPipeline(config, _storage)
                        await pipeline.ingest_text(
                            text=texto,
                            title=f"Sentencia {tipo_letra}-{numero}/{str(anio)[-2:]}",
                            source=sent_data.get("fuente_url", "corte_cc"),
                            doc_type="jurisprudencia",
                        )

                    if _derogation_graph:
                        from derogation.models import JurisprudenciaCreate, Corte, FuenteLegal
                        jc = JurisprudenciaCreate(
                            corte=Corte.CORTE_CONSTITUCIONAL,
                            tipo_sentencia=tipo_letra,
                            numero=f"{tipo_letra}-{numero}/{str(anio)[-2:]}",
                            fecha=None,
                            magistrado=sent_data.get("magistrado"),
                            fuente_url=sent_data.get("fuente_url", ""),
                            fuente=FuenteLegal.CORTE_CC,
                            texto_completo=texto[:5000],
                            decision=titulo[:500],
                        )
                        try:
                            await _derogation_graph.insert_jurisprudencia(jc)
                        except Exception as e:
                            logger.debug(f"jurisprudencia insert skipped: {e}")

                    ingested.append(label)
                    sentencias_frontend.append({
                        "titulo": f"Sentencia {tipo_letra}-{numero}/{str(anio)[-2:]}",
                        "source": "corte_cc",
                        "url": sent_data.get("fuente_url", ""),
                        "preview": (titulo or label)[:200],
                        "magistrado": sent_data.get("magistrado") or "",
                    })
                    logger.info(f"LLM-identified sentencia ingested: {label}")
                except Exception as e:
                    logger.warning(f"Failed to ingest LLM-identified sentencia {label}: {e}")

            return ingested, sentencias_frontend
        except Exception as e:
            logger.warning(f"LLM norm identification failed: {e}")
            return [], []

    async def _auto_ingest_live_results(self, live_results: list) -> bool:
        """Auto-ingest norms found in live sources that aren't already in the RAG.
        Returns True if anything was ingested."""
        try:
            from api.legal import _source_router, _derogation_graph, _storage, _embedder
        except ImportError:
            return False

        if not _source_router or not _storage:
            return False

        loaded_titles = await self._get_loaded_doc_titles()
        loaded_lower = {t.lower() for t in loaded_titles}
        ingested_any = False

        for lr in live_results:
            tipo = getattr(lr, 'tipo', None)
            numero = getattr(lr, 'numero', None)
            anio = getattr(lr, 'anio', None)

            if not tipo or not numero or not anio:
                continue

            nombre = f"{tipo} {numero} de {anio}"
            # Check if already loaded
            already = any(
                str(numero) in t and str(anio) in t
                for t in loaded_lower
            )
            if already:
                continue

            logger.info(f"Auto-ingesting from live results: {nombre}")
            try:
                norm_data = await _source_router.fetch_norm(tipo, int(numero), int(anio))
                if not norm_data or not norm_data.get("texto_completo"):
                    continue

                # Ingest to graph
                if _derogation_graph:
                    from derogation.models import NormaCreate, TipoNorma, FuenteLegal
                    from derogation.detector import detect_derogations
                    from derogation.models import DerogacionCreate

                    try:
                        norma_tipo = TipoNorma(tipo.upper())
                    except ValueError:
                        norma_tipo = TipoNorma.LEY

                    norma_create = NormaCreate(
                        tipo=norma_tipo, numero=int(numero), anio=int(anio),
                        titulo=norm_data.get("titulo", nombre),
                        fuente=FuenteLegal(norm_data.get("fuente", "manual")) if norm_data.get("fuente") in [e.value for e in FuenteLegal] else FuenteLegal.MANUAL,
                        fuente_url=norm_data.get("fuente_url"),
                        texto_completo=norm_data.get("texto_completo"),
                        metadata=norm_data.get("metadata", {}),
                    )

                    embedding = None
                    if _embedder:
                        embed_text = f"{norm_data.get('titulo', '')} {norm_data.get('texto_completo', '')[:1000]}"
                        embeddings = await _embedder.generate_embeddings_batch([embed_text])
                        embedding = embeddings[0] if embeddings else None

                    await _derogation_graph.insert_norma(norma_create, embedding=embedding)

                    # Detect derogations
                    derogations = detect_derogations(norm_data.get("texto_completo", ""))
                    for det in derogations:
                        if det.norma_afectada_numero and det.norma_afectada_anio:
                            affected = await _derogation_graph.get_norma(
                                det.norma_afectada_tipo or tipo,
                                det.norma_afectada_numero, det.norma_afectada_anio,
                            )
                            if affected:
                                origin = await _derogation_graph.get_norma(tipo, int(numero), int(anio))
                                if origin:
                                    derog = DerogacionCreate(
                                        norma_origen_id=str(origin["id"]),
                                        norma_destino_id=str(affected["id"]),
                                        tipo=det.tipo_derogacion,
                                        articulos_afectados=det.articulos_afectados,
                                        fuente_texto=det.texto_fuente,
                                        detectado_por="auto_regex",
                                        confianza=det.confianza,
                                    )
                                    await _derogation_graph.insert_derogacion(derog)

                # Ingest to RAG
                from ingestion.pipeline import IngestionPipeline
                from config.schema import load_config
                config = load_config()
                pipeline = IngestionPipeline(config, _storage)
                await pipeline.ingest_text(
                    text=norm_data.get("texto_completo", ""),
                    title=norm_data.get("titulo", nombre),
                    source=norm_data.get("fuente_url", "legal_source"),
                    doc_type="legal_norm",
                )
                ingested_any = True
                logger.info(f"Auto-ingested from live: {nombre}")

            except Exception as e:
                logger.error(f"Auto-ingest live failed for {nombre}: {e}")

        return ingested_any

    def _is_legal_mode(self) -> bool:
        """Check if the agent is configured for legal mode."""
        role = (self.config.agent.role or "").lower()
        return any(kw in role for kw in ("legal", "abogado", "jurídic", "juridic", "derecho", "lawyer"))

    async def _enrich_with_legal_sources(self, query: str, retrieval_result) -> tuple:
        """Search live legal sources and verify vigencia in parallel.

        Returns:
            (live_results, vigencia_results) — both can be None if not available
        """
        import re

        live_results = None
        vigencia_results = None

        try:
            from api.legal import _source_router, _vigencia_checker
        except ImportError:
            logger.debug("Legal API not available for enrichment")
            return None, None

        # Task 1: Search live sources
        if _source_router:
            try:
                result = await _source_router.search(query, limit=5)
                live_results = result.get("results", [])
                logger.info("Legal live search: %d results", len(live_results))
            except Exception as e:
                logger.warning("Live source search failed: %s", e)

        # Task 2: Extract ALL norm references from query + RAG results + document titles
        if _vigencia_checker:
            try:
                normas = []
                patterns = [
                    (r"(?:[Ll]ey)\s+(\d+)\s+(?:de\s+)?(\d{4})", "LEY"),
                    (r"(?:[Dd]ecreto)\s+(\d+)\s+(?:de\s+)?(\d{4})", "DECRETO"),
                    (r"(?:[Rr]esoluci[oó]n)\s+(\d+)\s+(?:de\s+)?(\d{4})", "RESOLUCION"),
                ]

                # Extract from user query
                for pattern, tipo in patterns:
                    for match in re.finditer(pattern, query):
                        normas.append({"tipo": tipo, "numero": match.group(1), "anio": match.group(2)})

                # Extract from RAG retrieval results (chunks content)
                for r in (retrieval_result.results or [])[:10]:
                    content = r.content or ""
                    for pattern, tipo in patterns:
                        for match in re.finditer(pattern, content):
                            normas.append({"tipo": tipo, "numero": match.group(1), "anio": match.group(2)})

                # Extract from document titles in sources
                for r in (retrieval_result.results or [])[:10]:
                    title = r.document_title or ""
                    for pattern, tipo in patterns:
                        for match in re.finditer(pattern, title):
                            normas.append({"tipo": tipo, "numero": match.group(1), "anio": match.group(2)})

                # Deduplicate
                seen = set()
                unique_normas = []
                for n in normas:
                    key = f"{n['tipo']}:{n['numero']}:{n['anio']}"
                    if key not in seen:
                        seen.add(key)
                        unique_normas.append(n)

                logger.info("Legal vigencia check: %d unique norms extracted", len(unique_normas))

                if unique_normas:
                    vigencia_results = await _vigencia_checker.verify_results(unique_normas[:15])
                    logger.info("Legal vigencia results: %d checked", len(vigencia_results))
            except Exception as e:
                logger.warning("Vigencia check failed: %s", e)

        return live_results, vigencia_results

    @staticmethod
    def new_session_id() -> str:
        """Generate a new session ID."""
        return str(uuid.uuid4())
