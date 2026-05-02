"""Code-aware chunker: splits source code by functions, classes, and logical blocks."""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

from models.chunks import ChunkingConfig, DocumentChunk

logger = logging.getLogger(__name__)

# Patterns for detecting code boundaries
FUNCTION_PATTERNS = {
    "python": r"^(class\s+\w+|def\s+\w+|async\s+def\s+\w+)",
    "javascript": r"^(export\s+)?(async\s+)?function\s+\w+|^(export\s+)?(const|let|var)\s+\w+\s*=\s*(async\s+)?\(",
    "typescript": r"^(export\s+)?(async\s+)?function\s+\w+|^(export\s+)?(const|let|var)\s+\w+\s*[=:]\s*(async\s+)?\(",
    "java": r"^\s*(public|private|protected|static|\s)+[\w<>\[\]]+\s+\w+\s*\(",
    "go": r"^func\s+(\(\w+\s+\*?\w+\)\s+)?\w+\s*\(",
    "rust": r"^(pub\s+)?(async\s+)?fn\s+\w+",
    "csharp": r"^\s*(public|private|protected|internal|static|\s)+[\w<>\[\]]+\s+\w+\s*\(",
    "ruby": r"^\s*(def\s+\w+|class\s+\w+|module\s+\w+)",
    "php": r"^\s*(public|private|protected|static|\s)*function\s+\w+",
}


def chunk(
    content: str,
    config: ChunkingConfig,
    title: str = "",
    source: str = "",
    metadata: Optional[Dict] = None,
) -> List[DocumentChunk]:
    """Split source code by functions/classes with import context preservation."""
    if not content or not content.strip():
        return []

    language = (metadata or {}).get("language", "python")
    base_metadata = {
        "title": title,
        "source": source,
        "chunk_method": "code",
        "language": language,
    }
    if metadata:
        base_metadata.update(metadata)

    # Extract imports/header section
    lines = content.split("\n")
    header_lines: List[str] = []
    body_start = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if _is_header_line(stripped, language):
            header_lines.append(line)
            body_start = i + 1
        elif stripped and not stripped.startswith("#") and not stripped.startswith("//"):
            break

    header = "\n".join(header_lines).strip()
    body = "\n".join(lines[body_start:])

    # Split body by function/class boundaries
    pattern = FUNCTION_PATTERNS.get(language, FUNCTION_PATTERNS["python"])
    blocks = _split_by_pattern(body, pattern)

    chunks: List[DocumentChunk] = []

    # Header as its own chunk if substantial
    if header and len(header) >= config.min_chunk_size:
        chunks.append(DocumentChunk(
            content=f"# Imports and declarations for {title or source}\n\n```{language}\n{header}\n```",
            index=len(chunks),
            metadata={**base_metadata, "section": "imports"},
        ))

    # Each code block as a chunk, prepending minimal header context
    header_summary = header[:200] + "..." if len(header) > 200 else header

    for block in blocks:
        block = block.strip()
        if not block or len(block) < config.min_chunk_size:
            continue

        # If block is too large, sub-split it
        if len(block) > config.max_chunk_size:
            sub_blocks = _split_large_block(block, config.chunk_size, config.chunk_overlap)
            for sub in sub_blocks:
                chunk_content = f"```{language}\n# Context: {title or source}\n{sub}\n```"
                chunks.append(DocumentChunk(
                    content=chunk_content,
                    index=len(chunks),
                    metadata={**base_metadata, "section": "code_block"},
                ))
        else:
            chunk_content = f"```{language}\n# Context: {title or source}\n{block}\n```"
            chunks.append(DocumentChunk(
                content=chunk_content,
                index=len(chunks),
                metadata={**base_metadata, "section": "code_block"},
            ))

    for c in chunks:
        c.metadata["total_chunks"] = len(chunks)

    logger.debug("CodeChunker: %d chunks from '%s' (%s)", len(chunks), title or source, language)
    return chunks


def _is_header_line(line: str, language: str) -> bool:
    """Check if a line is an import/header statement."""
    if not line:
        return True
    header_keywords = {
        "python": ("import ", "from ", "#!"),
        "javascript": ("import ", "require(", "const ", "'use strict'"),
        "typescript": ("import ", "require(", "const ", "'use strict'"),
        "java": ("import ", "package "),
        "go": ("import ", "package "),
        "rust": ("use ", "extern ", "mod "),
        "csharp": ("using ",),
        "ruby": ("require ", "require_relative "),
        "php": ("use ", "namespace ", "require ", "include "),
    }
    keywords = header_keywords.get(language, header_keywords["python"])
    return any(line.startswith(kw) for kw in keywords)


def _split_by_pattern(text: str, pattern: str) -> List[str]:
    """Split text at lines matching a regex pattern."""
    lines = text.split("\n")
    blocks: List[str] = []
    current: List[str] = []

    for line in lines:
        if re.match(pattern, line, re.MULTILINE) and current:
            blocks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)

    if current:
        blocks.append("\n".join(current))

    return blocks


def _split_large_block(text: str, size: int, overlap: int) -> List[str]:
    """Split a large text block by size with overlap."""
    parts = []
    start = 0
    while start < len(text):
        end = start + size
        parts.append(text[start:end])
        start = end - overlap
    return parts
