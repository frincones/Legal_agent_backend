"""Abstract base class for storage backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from models.chunks import DocumentChunk
from models.documents import Document
from models.search import SearchResult


class BaseStorage(ABC):
    """Interface for storage backends (Supabase, PostgreSQL, etc.)."""

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize connection pool and ensure schema exists."""

    @abstractmethod
    async def close(self) -> None:
        """Close all connections."""

    # -- Document operations --

    @abstractmethod
    async def save_document(
        self,
        document: Document,
        chunks: List[DocumentChunk],
    ) -> str:
        """Save a document and its chunks. Returns document ID."""

    @abstractmethod
    async def get_document(self, document_id: str) -> Optional[Document]:
        """Retrieve a document by ID."""

    @abstractmethod
    async def get_document_by_title(self, title: str) -> Optional[Document]:
        """Retrieve a document by title (case-insensitive)."""

    @abstractmethod
    async def list_documents(self) -> List[Dict[str, Any]]:
        """List all documents with metadata."""

    @abstractmethod
    async def delete_document(self, document_id: str) -> bool:
        """Delete a document and its chunks."""

    @abstractmethod
    async def clear_all(self) -> None:
        """Delete all documents and chunks."""

    # -- Search operations --

    @abstractmethod
    async def vector_search(
        self,
        query_embedding: List[float],
        limit: int = 10,
        similarity_threshold: float = 0.7,
    ) -> List[SearchResult]:
        """Perform vector similarity search over document chunks."""

    @abstractmethod
    async def hybrid_search(
        self,
        query_embedding: List[float],
        query_text: str,
        limit: int = 10,
        text_weight: float = 0.3,
        similarity_threshold: float = 0.5,
    ) -> List[SearchResult]:
        """Perform hybrid search (vector + BM25 text)."""

    # -- Chat history (lightweight, always-on) --

    @abstractmethod
    async def save_chat_message(
        self,
        session_id: str,
        user_message: str,
        assistant_message: str,
        intent: Optional[str] = None,
        sources_used: Optional[List[str]] = None,
    ) -> str:
        """Persist a chat exchange WITHOUT embedding (for in-session context)."""

    # -- Conversation memory operations (heavyweight, opt-in) --

    @abstractmethod
    async def save_conversation(
        self,
        session_id: str,
        user_message: str,
        assistant_message: str,
        embedding: List[float],
        intent: Optional[str] = None,
        sources_used: Optional[List[str]] = None,
        relevance_score: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Save a conversation exchange and embed it for future retrieval."""

    @abstractmethod
    async def search_conversations(
        self,
        query_embedding: List[float],
        limit: int = 5,
        similarity_threshold: float = 0.7,
        session_id: Optional[str] = None,
    ) -> List[SearchResult]:
        """Search past conversations by semantic similarity."""

    @abstractmethod
    async def search_all(
        self,
        query_embedding: List[float],
        doc_limit: int = 5,
        conv_limit: int = 3,
        similarity_threshold: float = 0.7,
    ) -> List[SearchResult]:
        """Search both documents and conversations."""

    # -- Session management --

    @abstractmethod
    async def list_sessions(self, limit: int = 100) -> List[Dict[str, Any]]:
        """List all chat sessions with metadata."""

    @abstractmethod
    async def get_session_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """Retrieve all messages from a session in chronological order."""

    @abstractmethod
    async def delete_session(self, session_id: str) -> bool:
        """Delete all conversations for a session."""
