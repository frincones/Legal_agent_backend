"""Docling HybridChunker: token-aware, structure-preserving chunking."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from models.chunks import ChunkingConfig, DocumentChunk
from ingestion.chunkers import simple_chunker

logger = logging.getLogger(__name__)


def chunk(
    content: str,
    config: ChunkingConfig,
    title: str = "",
    source: str = "",
    metadata: Optional[Dict] = None,
    docling_doc: Optional[object] = None,
) -> List[DocumentChunk]:
    """
    Chunk using Docling HybridChunker if a DoclingDocument is available.
    Falls back to simple chunker otherwise.
    """
    if docling_doc is None:
        logger.debug("No DoclingDocument provided, falling back to simple chunker")
        return simple_chunker.chunk(content, config, title, source, metadata)

    try:
        from docling.chunking import HybridChunker
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(
            "sentence-transformers/all-MiniLM-L6-v2"
        )

        chunker = HybridChunker(
            tokenizer=tokenizer,
            max_tokens=config.max_tokens,
            merge_peers=config.merge_peers,
        )

        raw_chunks = list(chunker.chunk(dl_doc=docling_doc))

        base_metadata = {"title": title, "source": source, "chunk_method": "hybrid"}
        if metadata:
            base_metadata.update(metadata)

        chunks: List[DocumentChunk] = []
        for i, raw_chunk in enumerate(raw_chunks):
            # Contextualize includes heading hierarchy
            try:
                text = chunker.contextualize(chunk=raw_chunk)
            except Exception:
                text = raw_chunk.text if hasattr(raw_chunk, "text") else str(raw_chunk)

            if not text or len(text.strip()) < config.min_chunk_size:
                continue

            token_count = len(tokenizer.encode(text, add_special_tokens=False))

            chunks.append(DocumentChunk(
                content=text,
                index=i,
                start_char=0,
                end_char=len(text),
                metadata={
                    **base_metadata,
                    "total_chunks": 0,
                    "token_count": token_count,
                    "has_context": True,
                },
                token_count=token_count,
            ))

        for c in chunks:
            c.metadata["total_chunks"] = len(chunks)

        logger.debug("HybridChunker: %d chunks from '%s'", len(chunks), title or source)
        return chunks

    except ImportError:
        logger.warning("Docling/transformers not installed. Falling back to simple chunker.")
        return simple_chunker.chunk(content, config, title, source, metadata)
    except Exception as e:
        logger.warning("HybridChunker failed: %s. Falling back to simple chunker.", e)
        return simple_chunker.chunk(content, config, title, source, metadata)
