"""Simple paragraph-based chunker with character overlap. Universal fallback."""

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
) -> List[DocumentChunk]:
    """Split content into chunks using paragraph boundaries with overlap."""
    if not content or not content.strip():
        return []

    base_metadata = {"title": title, "source": source, "chunk_method": "simple"}
    if metadata:
        base_metadata.update(metadata)

    # Split by paragraphs first
    raw_paragraphs = re.split(r"\n\n+", content.strip())

    # Hard-cap any oversized paragraph: split it by character size so a single
    # giant paragraph doesn't produce a single huge chunk.
    paragraphs: List[str] = []
    for p in raw_paragraphs:
        if len(p) <= config.chunk_size:
            paragraphs.append(p)
        else:
            # Force-split oversized paragraph
            for i in range(0, len(p), config.chunk_size):
                paragraphs.append(p[i:i + config.chunk_size])

    chunks: List[DocumentChunk] = []
    current_text = ""
    current_start = 0
    char_pos = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            char_pos += 2
            continue

        # If adding this paragraph exceeds chunk size, save current and start new
        if current_text and len(current_text) + len(para) + 2 > config.chunk_size:
            chunks.append(DocumentChunk(
                content=current_text.strip(),
                index=len(chunks),
                start_char=current_start,
                end_char=current_start + len(current_text),
                metadata={**base_metadata, "total_chunks": 0},
            ))

            # Overlap: keep tail of current text
            overlap_text = current_text[-config.chunk_overlap:] if config.chunk_overlap > 0 else ""
            # Find sentence boundary in overlap
            sentence_break = overlap_text.rfind(". ")
            if sentence_break > 0:
                overlap_text = overlap_text[sentence_break + 2:]

            current_start = char_pos - len(overlap_text)
            current_text = overlap_text + "\n\n" + para if overlap_text else para
        else:
            if current_text:
                current_text += "\n\n" + para
            else:
                current_text = para
                current_start = char_pos

        char_pos += len(para) + 2

    # Last chunk
    if current_text.strip():
        chunks.append(DocumentChunk(
            content=current_text.strip(),
            index=len(chunks),
            start_char=current_start,
            end_char=current_start + len(current_text),
            metadata={**base_metadata, "total_chunks": 0},
        ))

    # Update total_chunks
    for c in chunks:
        c.metadata["total_chunks"] = len(chunks)

    # Filter out chunks that are too small
    chunks = [c for c in chunks if len(c.content) >= config.min_chunk_size]

    logger.debug("SimpleChunker: %d chunks from '%s'", len(chunks), title or source)
    return chunks
