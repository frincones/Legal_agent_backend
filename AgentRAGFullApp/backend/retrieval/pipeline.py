"""Retrieval pipeline orchestrator: RAG-first with self-reflection (Level 5)."""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from config.schema import AppConfig
from ingestion.embedder import EmbeddingGenerator, create_embedder
from models.search import IntentType, RetrievalResult, SearchQuery, SearchResult, SearchType
from retrieval.intent_router import classify_intent
from retrieval.query_expansion import expand_query
from retrieval.multi_query import multi_query_search
from retrieval.reranker import rerank
from retrieval.self_reflection import evaluate_and_refine
from retrieval.sql_generator import generate_and_execute
from storage.base import BaseStorage

logger = logging.getLogger(__name__)


class RetrievalPipeline:
    """
    Level 5 retrieval pipeline:
    1. Intent Router (classify query type)
    2. Query Expansion (enrich query)
    3. Multi-Query parallel search
    4. Re-ranking (cross-encoder precision)
    5. Self-Reflection (evaluate + refine if needed)

    Always fetches data BEFORE the main LLM responds.
    """

    def __init__(self, config: AppConfig, storage: BaseStorage):
        self.config = config
        self.storage = storage
        self.embedder = create_embedder(
            model=config.ingestion.embedding.model,
            use_cache=config.ingestion.embedding.use_cache,
        )
        self.retrieval_cfg = config.retrieval

    async def retrieve(
        self,
        query: str,
        session_id: Optional[str] = None,
    ) -> RetrievalResult:
        """Execute the full retrieval pipeline."""
        start = time.time()

        search_query = SearchQuery(original_query=query)
        result = RetrievalResult(query=search_query)

        # ── Step 1: Intent Router ──
        if self.retrieval_cfg.intent_router.enabled:
            search_query.intent = await classify_intent(
                query,
                model=self.retrieval_cfg.intent_router.model,
                db_tables_schema=self.config.agent.db_tables_schema,
            )
        else:
            search_query.intent = IntentType.KNOWLEDGE

        logger.info("Intent: %s for query: '%s'", search_query.intent.value, query[:60])

        # ── Step 2: Route based on intent ──
        if search_query.intent == IntentType.CONVERSATION:
            # No data fetching needed for casual chat
            result.duration_ms = (time.time() - start) * 1000
            return result

        if search_query.intent in (IntentType.KNOWLEDGE, IntentType.HYBRID):
            result.results = await self._knowledge_retrieval(query, search_query)

        if search_query.intent in (IntentType.STRUCTURED, IntentType.HYBRID):
            result.sql_data = await self._structured_retrieval(query)

        if search_query.intent == IntentType.ACTION:
            # For actions, still do knowledge search for context
            result.results = await self._knowledge_retrieval(query, search_query)

        # ── Step 5: Self-Reflection ──
        if self.retrieval_cfg.self_reflection.enabled and result.results:
            score, refined_query = await evaluate_and_refine(
                query=query,
                results=result.results,
                model=self.retrieval_cfg.self_reflection.model,
                threshold=self.retrieval_cfg.self_reflection.threshold,
            )
            result.reflection_score = score

            if refined_query and score < self.retrieval_cfg.self_reflection.threshold:
                # Re-search with refined query (max 1 retry)
                logger.info("Self-reflection: score=%d, refining query", score)
                result.was_refined = True
                result.refined_query = refined_query
                result.results = await self._knowledge_retrieval(
                    refined_query, search_query, skip_expansion=True,
                )

        # Also search conversation memory if explicitly enabled.
        # Disabled by default because past conversations as RAG context tend
        # to confuse the LLM with irrelevant prior exchanges.
        if session_id and self.retrieval_cfg.conversation_memory.enabled:
            try:
                conv_results = await self.storage.search_conversations(
                    query_embedding=await self.embedder.generate_embedding(query),
                    limit=3,
                    session_id=None,
                )
                if conv_results:
                    result.results.extend(conv_results)
                    result.results.sort(key=lambda x: x.best_score, reverse=True)
            except Exception as e:
                logger.debug("Conversation memory search failed: %s", e)

        result.duration_ms = (time.time() - start) * 1000
        logger.info(
            "Retrieval complete: %d results, %.0fms, intent=%s, reflection=%s",
            len(result.results), result.duration_ms,
            search_query.intent.value,
            result.reflection_score,
        )

        return result

    async def _knowledge_retrieval(
        self,
        query: str,
        search_query: SearchQuery,
        skip_expansion: bool = False,
    ) -> List[SearchResult]:
        """Execute the knowledge retrieval sub-pipeline."""

        effective_query = query

        # ── Step 2: Query Expansion ──
        if self.retrieval_cfg.query_expansion.enabled and not skip_expansion:
            expanded = await expand_query(
                query, model=self.retrieval_cfg.query_expansion.model
            )
            search_query.expanded_query = expanded
            effective_query = expanded

        # ── Step 3: Multi-Query or Single Search ──
        rerank_cfg = self.retrieval_cfg.reranking
        candidates_limit = rerank_cfg.candidates if rerank_cfg.enabled else rerank_cfg.top_k

        if self.retrieval_cfg.multi_query.enabled:
            search_query.search_type = SearchType.MULTI_QUERY
            results = await multi_query_search(
                query=effective_query,
                storage=self.storage,
                embedder=self.embedder,
                num_variations=self.retrieval_cfg.multi_query.num_variations,
                limit=candidates_limit,
                model=self.retrieval_cfg.query_expansion.model,
                parallel=self.retrieval_cfg.multi_query.parallel,
            )
        elif self.retrieval_cfg.hybrid_search.enabled:
            search_query.search_type = SearchType.HYBRID
            embedding = await self.embedder.generate_embedding(effective_query)
            results = await self.storage.hybrid_search(
                query_embedding=embedding,
                query_text=query,
                limit=candidates_limit,
                text_weight=self.retrieval_cfg.hybrid_search.text_weight,
            )
        else:
            search_query.search_type = SearchType.SEMANTIC
            embedding = await self.embedder.generate_embedding(effective_query)
            results = await self.storage.vector_search(
                query_embedding=embedding,
                limit=candidates_limit,
            )

        # ── Step 4: Re-ranking ──
        if self.retrieval_cfg.reranking.enabled and results:
            search_query.search_type = SearchType.RERANKED
            results = rerank(
                query=query,
                results=results,
                top_k=rerank_cfg.top_k,
                model_name=rerank_cfg.model,
            )

        return results

    async def _structured_retrieval(self, query: str):
        """Execute structured data retrieval via SQL generation."""
        if not self.config.agent.db_tables_schema:
            logger.debug("No db_tables_schema configured. Skipping SQL retrieval.")
            return None

        return await generate_and_execute(
            question=query,
            storage=self.storage,
            tables_schema=self.config.agent.db_tables_schema,
            model=self.retrieval_cfg.intent_router.model,
        )
