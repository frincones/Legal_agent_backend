"""Self-reflective RAG: evaluates retrieval quality and refines if needed."""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from models.search import SearchResult
from utils.llm import llm_generate

logger = logging.getLogger(__name__)

GRADE_PROMPT = """You are evaluating whether retrieved documents are relevant to a user's query.

Query: {query}

Retrieved documents (first 500 chars each):
{documents}

Grade the overall relevance of these results on a scale of 1-5:
1 = Completely irrelevant
2 = Mostly irrelevant
3 = Somewhat relevant
4 = Mostly relevant
5 = Highly relevant

Respond with ONLY the number."""

REFINE_PROMPT = """The search query "{query}" returned low-relevance results from a knowledge base.

Suggest an improved search query that would find more relevant results. Consider:
- Alternative terminology
- More specific or more general phrasing
- Different aspects of the same topic

Respond with ONLY the improved query, nothing else."""


async def evaluate_and_refine(
    query: str,
    results: List[SearchResult],
    model: str = "gpt-4o-mini",
    threshold: int = 3,
) -> Tuple[int, Optional[str]]:
    """
    Evaluate retrieval quality and suggest a refined query if needed.
    Returns (score, refined_query_or_none).
    """
    if not results:
        return 1, await _refine_query(query, model)

    # Build document summary for grading
    doc_summary = ""
    for i, r in enumerate(results[:5], 1):
        doc_summary += f"\n[{i}] {r.content[:500]}\n"

    try:
        grade_response = await llm_generate(
            prompt=GRADE_PROMPT.format(query=query, documents=doc_summary),
            model=model,
            temperature=0.0,
            max_tokens=5,
            purpose="self_reflection_grade",
        )

        score = int(grade_response.strip().split()[0])
        score = max(1, min(5, score))

        logger.debug("Self-reflection score: %d/5 for query '%s'", score, query[:50])

        if score < threshold:
            refined = await _refine_query(query, model)
            return score, refined

        return score, None

    except (ValueError, IndexError):
        logger.warning("Could not parse reflection score. Assuming adequate.")
        return 3, None


async def _refine_query(query: str, model: str) -> str:
    """Generate a refined search query."""
    try:
        refined = await llm_generate(
            prompt=REFINE_PROMPT.format(query=query),
            model=model,
            temperature=0.3,
            max_tokens=100,
            purpose="self_reflection_refine",
        )
        logger.debug("Refined query: '%s' -> '%s'", query[:50], refined[:80])
        return refined.strip()
    except Exception as e:
        logger.warning("Query refinement failed: %s", e)
        return query
