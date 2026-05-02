"""Re-ranking: cross-encoder model for precise relevance scoring."""

from __future__ import annotations

import logging
from typing import List, Optional

from models.search import SearchResult

logger = logging.getLogger(__name__)

_reranker = None


def _get_reranker(model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
    """Lazy-load the cross-encoder model."""
    global _reranker
    if _reranker is None:
        try:
            from sentence_transformers import CrossEncoder
            _reranker = CrossEncoder(model_name)
            logger.info("Loaded re-ranker model: %s", model_name)
        except ImportError:
            logger.error("sentence-transformers not installed. Install: pip install sentence-transformers")
            raise
    return _reranker


def rerank(
    query: str,
    results: List[SearchResult],
    top_k: int = 5,
    model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
) -> List[SearchResult]:
    """Re-rank search results using a cross-encoder model."""
    if not results:
        return results

    if len(results) <= top_k:
        return results

    try:
        reranker = _get_reranker(model_name)

        # Create query-document pairs
        pairs = [[query, r.content] for r in results]

        # Get cross-encoder scores
        scores = reranker.predict(pairs)

        # Attach rerank scores and sort
        for result, score in zip(results, scores):
            result.rerank_score = float(score)

        reranked = sorted(results, key=lambda x: x.rerank_score or 0, reverse=True)

        logger.debug(
            "Re-ranked %d results -> top %d (best score: %.3f)",
            len(results), top_k,
            reranked[0].rerank_score if reranked else 0,
        )

        return reranked[:top_k]

    except ImportError:
        logger.warning("Re-ranking unavailable. Returning original results.")
        return results[:top_k]
    except Exception as e:
        logger.warning("Re-ranking failed: %s. Returning original results.", e)
        return results[:top_k]
