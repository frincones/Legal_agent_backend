"""Data models for documents and ingestion results."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class Document:
    """Represents a document in the knowledge base."""

    title: str
    source: str
    content: str
    doc_type: str = "unknown"
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None
    id: Optional[str] = None

    @property
    def word_count(self) -> int:
        return len(self.content.split())

    @property
    def char_count(self) -> int:
        return len(self.content)


@dataclass
class IngestionResult:
    """Result of ingesting one or more documents."""

    total_documents: int = 0
    successful: int = 0
    failed: int = 0
    total_chunks: int = 0
    errors: List[Dict[str, str]] = field(default_factory=list)
    documents_processed: List[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def success_rate(self) -> float:
        if self.total_documents == 0:
            return 0.0
        return self.successful / self.total_documents

    def add_error(self, file_path: str, error: str) -> None:
        self.failed += 1
        self.errors.append({"file": file_path, "error": error})

    def add_success(self, file_path: str, chunks: int) -> None:
        self.successful += 1
        self.total_chunks += chunks
        self.documents_processed.append(file_path)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_documents": self.total_documents,
            "successful": self.successful,
            "failed": self.failed,
            "total_chunks": self.total_chunks,
            "success_rate": f"{self.success_rate:.1%}",
            "duration_seconds": round(self.duration_seconds, 2),
            "errors": self.errors,
        }
