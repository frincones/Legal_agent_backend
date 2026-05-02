"""Multi-query RAG: generates multiple query variations for broader coverage."""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List

from models.search import SearchResult
from storage.base import BaseStorage
from ingestion.embedder import EmbeddingGenerator
from utils.llm import llm_generate

logger = logging.getLogger(__name__)

VARIATION_PROMPT = """Generate {num_variations} different search query variations for the following question.
Each variation should approach the question from a different angle or use different terminology.
Return ONLY the queries, one per line, no numbering or bullets.

Original question: "{query}"

Variations:"""


async def generate_query_variations(
    query: str,
    num_variations: int = 3,
    model: str = "gpt-4o-mini",
) -> List[str]:
    """Generate multiple query variations using LLM."""
    try:
        response = await llm_generate(
            prompt=VARIATION_PROMPT.format(query=query, num_variations=num_variations),
            model=model,
            temperature=0.5,
            max_tokens=300,
            purpose="multi_query_variations",
        )

        variations = [line.strip() for line in response.strip().split("\n") if line.strip()]
        # Always include the original query
        all_queries = [query] + variations[:num_variations]
        logger.debug("Generated %d query variations", len(all_queries))
        return all_queries

    except Exception as e:
        logger.warning("Query variation generation failed: %s", e)
        return [query]


async def multi_query_search(
    query: str,
    storage: BaseStorage,
    embedder: EmbeddingGenerator,
    num_variations: int = 3,
    limit: int = 5,
    model: str = "gpt-4o-mini",
    parallel: bool = True,
) -> List[SearchResult]:
    """Execute searches with multiple query variations and deduplicate."""
    queries = await generate_query_variations(query, num_variations, model)

    if parallel:
        # Generate all embeddings in parallel
        embedding_tasks = [embedder.generate_embedding(q) for q in queries]
        embeddings = await asyncio.gather(*embedding_tasks)

        # Execute all searches in parallel
        search_tasks = [
            storage.vector_search(emb, limit=limit)
            for emb in embeddings
        ]
        results_lists = await asyncio.gather(*search_tasks)
    else:
        results_lists = []
        for q in queries:
            emb = await embedder.generate_embedding(q)
            results = await storage.vector_search(emb, limit=limit)
            results_lists.append(results)

    # Deduplicate: keep highest similarity per chunk_id
    seen: Dict[str, SearchResult] = {}
    for results in results_lists:
        for r in results:
            if r.chunk_id not in seen or r.similarity > seen[r.chunk_id].similarity:
                seen[r.chunk_id] = r

    # Sort by similarity and return top results
    deduped = sorted(seen.values(), key=lambda x: x.similarity, reverse=True)[:limit]

    logger.debug(
        "Multi-query: %d queries -> %d unique results (from %d total)",
        len(queries), len(deduped),
        sum(len(r) for r in results_lists),
    )
    return deduped
