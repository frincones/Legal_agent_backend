"""Semantic chunker: splits by similarity between consecutive sentences."""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

from models.chunks import ChunkingConfig, DocumentChunk

logger = logging.getLogger(__name__)


def chunk(
    content: str,
    config: ChunkingConfig,
    title: str = "",
    source: str = "",
    metadata: Optional[Dict] = None,
    similarity_threshold: float = 0.8,
) -> List[DocumentChunk]:
    """Split content at semantic boundaries using embedding similarity."""
    if not content or not content.strip():
        return []

    base_metadata = {"title": title, "source": source, "chunk_method": "semantic"}
    if metadata:
        base_metadata.update(metadata)

    sentences = _split_sentences(content)
    if len(sentences) <= 1:
        return [DocumentChunk(
            content=content,
            index=0,
            metadata={**base_metadata, "total_chunks": 1},
        )]

    try:
        from sentence_transformers import SentenceTransformer
        import numpy as np

        model = SentenceTransformer("all-MiniLM-L6-v2")
        embeddings = model.encode(sentences)

        # Calculate similarity between consecutive sentences
        similarities = []
        for i in range(len(embeddings) - 1):
            sim = float(np.dot(embeddings[i], embeddings[i + 1]) / (
                np.linalg.norm(embeddings[i]) * np.linalg.norm(embeddings[i + 1])
            ))
            similarities.append(sim)

        # Find split points where similarity drops below threshold
        split_indices = [0]
        for i, sim in enumerate(similarities):
            if sim < similarity_threshold:
                split_indices.append(i + 1)
        split_indices.append(len(sentences))

        # Build chunks from split points
        chunks: List[DocumentChunk] = []
        for i in range(len(split_indices) - 1):
            start_idx = split_indices[i]
            end_idx = split_indices[i + 1]
            chunk_text = " ".join(sentences[start_idx:end_idx]).strip()

            if len(chunk_text) < config.min_chunk_size and chunks:
                # Merge with previous chunk
                chunks[-1].content += " " + chunk_text
                continue

            # If chunk is too large, split it further
            if len(chunk_text) > config.max_chunk_size:
                sub_chunks = _split_large_text(chunk_text, config.chunk_size, config.chunk_overlap)
                for sub in sub_chunks:
                    chunks.append(DocumentChunk(
                        content=sub,
                        index=len(chunks),
                        metadata={**base_metadata},
                    ))
            else:
                chunks.append(DocumentChunk(
                    content=chunk_text,
                    index=len(chunks),
                    metadata={**base_metadata},
                ))

        for c in chunks:
            c.metadata["total_chunks"] = len(chunks)

        logger.debug("SemanticChunker: %d chunks from '%s'", len(chunks), title or source)
        return chunks

    except ImportError:
        logger.warning("sentence-transformers not installed. Falling back to simple chunking.")
        from ingestion.chunkers import simple_chunker
        return simple_chunker.chunk(content, config, title, source, metadata)


def _split_sentences(text: str) -> List[str]:
    """Split text into sentences."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return [s.strip() for s in sentences if s.strip()]


def _split_large_text(text: str, size: int, overlap: int) -> List[str]:
    """Split large text by character size with overlap."""
    parts = []
    start = 0
    while start < len(text):
        end = start + size
        # Try to break at sentence boundary
        if end < len(text):
            last_period = text.rfind(". ", start, end)
            if last_period > start:
                end = last_period + 2
        parts.append(text[start:end].strip())
        start = end - overlap
    return [p for p in parts if p]
