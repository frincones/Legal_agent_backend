"""Query expansion: enriches user query for better retrieval precision."""

from __future__ import annotations

import logging

from utils.llm import llm_generate

logger = logging.getLogger(__name__)

EXPANSION_PROMPT = """You are a query expansion assistant. Take the user's brief query and expand it into a more detailed, comprehensive version that:
1. Adds relevant context and clarifications
2. Includes related terminology and concepts
3. Specifies what aspects should be covered
4. Maintains the original intent
5. Keeps it as a single, coherent question

Expand the query to be 2-3x more detailed while staying focused.

Original query: "{query}"

Expanded query:"""


async def expand_query(
    query: str,
    model: str = "gpt-4o-mini",
) -> str:
    """Expand a brief query into a more detailed version."""
    try:
        expanded = await llm_generate(
            prompt=EXPANSION_PROMPT.format(query=query),
            model=model,
            temperature=0.3,
            max_tokens=200,
            purpose="query_expansion",
        )
        logger.debug("Query expanded: '%s' -> '%s'", query[:50], expanded[:80])
        return expanded
    except Exception as e:
        logger.warning("Query expansion failed: %s. Using original.", e)
        return query
