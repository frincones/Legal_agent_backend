"""Data models for search queries and results."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from enum import Enum


class SearchType(str, Enum):
    SEMANTIC = "semantic"
    HYBRID = "hybrid"
    MULTI_QUERY = "multi_query"
    RERANKED = "reranked"


class IntentType(str, Enum):
    KNOWLEDGE = "knowledge"
    STRUCTURED = "structured"
    HYBRID = "hybrid"
    ACTION = "action"
    CONVERSATION = "conversation"


@dataclass
class SearchQuery:
    """Represents a search query with metadata."""

    original_query: str
    expanded_query: Optional[str] = None
    query_variations: List[str] = field(default_factory=list)
    intent: Optional[IntentType] = None
    search_type: SearchType = SearchType.SEMANTIC
    timestamp: Optional[datetime] = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


@dataclass
class SearchResult:
    """A single search result from the knowledge base."""

    chunk_id: str
    document_id: str
    content: str
    similarity: float
    metadata: Dict[str, Any] = field(default_factory=dict)
    document_title: str = ""
    document_source: str = ""
    rerank_score: Optional[float] = None

    @property
    def best_score(self) -> float:
        return self.rerank_score if self.rerank_score is not None else self.similarity


@dataclass
class RetrievalResult:
    """Complete result of a retrieval pipeline execution."""

    query: SearchQuery
    results: List[SearchResult] = field(default_factory=list)
    reflection_score: Optional[int] = None
    was_refined: bool = False
    refined_query: Optional[str] = None
    sql_data: Optional[List[Dict[str, Any]]] = None
    duration_ms: float = 0.0

    @property
    def has_results(self) -> bool:
        return len(self.results) > 0 or (self.sql_data is not None and len(self.sql_data) > 0)

    def format_context(self) -> str:
        """Format results as context for LLM consumption."""
        parts = []

        if self.results:
            parts.append("## Retrieved Knowledge")
            for i, r in enumerate(self.results, 1):
                source = f" (Source: {r.document_title})" if r.document_title else ""
                score = f" [relevance: {r.best_score:.2f}]"
                parts.append(f"\n### Result {i}{source}{score}\n{r.content}")

        if self.sql_data:
            parts.append("\n## Structured Data")
            for row in self.sql_data:
                parts.append(str(row))

        if self.was_refined:
            parts.append(f"\n_Note: Query was refined from '{self.query.original_query}' to '{self.refined_query}'_")

        return "\n".join(parts) if parts else "No relevant information found."
