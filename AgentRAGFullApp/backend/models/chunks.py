"""Data models for document chunks and chunking configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ChunkingConfig:
    """Configuration for document chunking."""

    chunk_size: int = 1000
    chunk_overlap: int = 200
    max_chunk_size: int = 2000
    min_chunk_size: int = 100
    max_tokens: int = 512
    use_semantic_splitting: bool = True
    preserve_structure: bool = True
    merge_peers: bool = True


@dataclass
class DocumentChunk:
    """A single chunk of a document with optional embedding."""

    content: str
    index: int
    start_char: int = 0
    end_char: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    token_count: Optional[int] = None
    embedding: Optional[List[float]] = None

    @property
    def has_embedding(self) -> bool:
        return self.embedding is not None and len(self.embedding) > 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "content": self.content,
            "index": self.index,
            "start_char": self.start_char,
            "end_char": self.end_char,
            "metadata": self.metadata,
            "token_count": self.token_count,
        }
