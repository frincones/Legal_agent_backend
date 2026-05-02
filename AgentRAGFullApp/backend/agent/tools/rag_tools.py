"""RAG tools: additional search and document retrieval for the agent."""

from __future__ import annotations

import logging
from typing import Optional

from ingestion.embedder import EmbeddingGenerator
from storage.base import BaseStorage

logger = logging.getLogger(__name__)


async def search_knowledge_base(
    query: str,
    storage: BaseStorage,
    embedder: EmbeddingGenerator,
    limit: int = 5,
) -> str:
    """Perform a direct vector search on the knowledge base."""
    embedding = await embedder.generate_embedding(query)
    results = await storage.vector_search(embedding, limit=limit)

    if not results:
        return "No relevant documents found for this query."

    formatted = []
    for i, r in enumerate(results, 1):
        source = f" (Source: {r.document_title})" if r.document_title else ""
        formatted.append(f"**Result {i}**{source} [score: {r.similarity:.2f}]\n{r.content}\n")

    return "\n---\n".join(formatted)


async def retrieve_full_document(
    title: str,
    storage: BaseStorage,
) -> str:
    """Retrieve the complete content of a document by title."""
    doc = await storage.get_document_by_title(title)
    if doc is None:
        return f"No document found matching '{title}'."

    return f"**{doc.title}** (Source: {doc.source})\n\n{doc.content}"


async def list_available_documents(
    storage: BaseStorage,
) -> str:
    """List all documents in the knowledge base."""
    docs = await storage.list_documents()
    if not docs:
        return "No documents in the knowledge base."

    lines = ["**Documents in Knowledge Base:**\n"]
    for d in docs:
        lines.append(f"- **{d['title']}** ({d['doc_type']}) - {d['chunk_count']} chunks")

    return "\n".join(lines)
