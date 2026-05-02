"""Contextual enrichment: LLM adds document context to each chunk before embedding."""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from models.chunks import DocumentChunk
from utils.llm import llm_generate

logger = logging.getLogger(__name__)

ENRICHMENT_PROMPT = """<document>
Title: {title}
Source: {source}

{document_excerpt}
</document>

<chunk>
{chunk_content}
</chunk>

Provide a brief, 1-2 sentence context explaining what this chunk discusses in relation to the overall document. Be concise and specific. Do not include preamble, just the context sentence(s)."""


async def enrich_chunk(
    chunk: DocumentChunk,
    document_content: str,
    title: str,
    source: str,
    model: str = "gpt-4o-mini",
    max_document_chars: int = 4000,
) -> DocumentChunk:
    """Add contextual prefix to a single chunk using LLM."""
    try:
        prompt = ENRICHMENT_PROMPT.format(
            title=title,
            source=source,
            document_excerpt=document_content[:max_document_chars],
            chunk_content=chunk.content,
        )

        context = await llm_generate(
            prompt=prompt,
            model=model,
            temperature=0.0,
            max_tokens=150,
            purpose="contextual_enrichment",
        )

        chunk.content = f"{context}\n\n---\n\n{chunk.content}"
        chunk.metadata["enriched"] = True
        return chunk

    except Exception as e:
        logger.warning("Enrichment failed for chunk %d: %s", chunk.index, e)
        chunk.metadata["enriched"] = False
        return chunk


async def enrich_chunks(
    chunks: List[DocumentChunk],
    document_content: str,
    title: str,
    source: str,
    model: str = "gpt-4o-mini",
    max_concurrent: int = 5,
    max_document_chars: int = 4000,
) -> List[DocumentChunk]:
    """Add contextual enrichment to all chunks with concurrency control."""
    if not chunks:
        return chunks

    semaphore = asyncio.Semaphore(max_concurrent)

    async def _enrich_with_limit(chunk: DocumentChunk) -> DocumentChunk:
        async with semaphore:
            return await enrich_chunk(
                chunk, document_content, title, source, model, max_document_chars
            )

    tasks = [_enrich_with_limit(c) for c in chunks]
    enriched = await asyncio.gather(*tasks, return_exceptions=True)

    results = []
    for i, result in enumerate(enriched):
        if isinstance(result, Exception):
            logger.warning("Enrichment exception for chunk %d: %s", i, result)
            chunks[i].metadata["enriched"] = False
            results.append(chunks[i])
        else:
            results.append(result)

    enriched_count = sum(1 for c in results if c.metadata.get("enriched"))
    logger.info("Enriched %d/%d chunks for '%s'", enriched_count, len(results), title)
    return results
