"""PostgreSQL + pgVector storage backend (works with Supabase)."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import asyncpg

from models.chunks import DocumentChunk
from models.documents import Document
from models.search import SearchResult
from storage.base import BaseStorage

logger = logging.getLogger(__name__)


def _embedding_to_pg(embedding: List[float]) -> str:
    """Convert Python list to PostgreSQL vector literal."""
    return "[" + ",".join(str(x) for x in embedding) + "]"


def _sanitize_text(value: Optional[str]) -> Optional[str]:
    """Strip NUL bytes (0x00) and other invalid UTF-8 codepoints.

    PostgreSQL TEXT columns reject NUL bytes. This happens when binary files
    (PDF, DOCX, etc.) are read as raw text by a fallback reader.
    """
    if value is None:
        return None
    if "\x00" in value:
        value = value.replace("\x00", "")
    return value


def _sanitize_metadata(value):
    """Recursively coerce metadata into JSON-serializable values.

    Document readers (especially Docling) may inject methods, datetime
    objects, custom classes, or Path objects into metadata. asyncpg + json.dumps
    will fail with 'Object of type X is not JSON serializable'. This makes
    the storage defensive: anything non-serializable becomes its string repr.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _sanitize_metadata(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_metadata(v) for v in value]
    if callable(value):
        # Methods, lambdas, functions, classes
        return None
    # datetime, Path, custom objects, etc → fallback to string
    try:
        # Try JSON serialization first as the cheapest test
        import json
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        try:
            return str(value)
        except Exception:
            return None


class PostgresStorage(BaseStorage):
    """PostgreSQL + pgVector storage backend."""

    def __init__(
        self,
        connection_string: str,
        pool_min: int = 2,
        pool_max: int = 10,
        similarity_threshold: float = 0.7,
    ):
        self.connection_string = connection_string
        self.pool_min = pool_min
        self.pool_max = pool_max
        self.similarity_threshold = similarity_threshold
        self._pool: Optional[asyncpg.Pool] = None

    async def initialize(self) -> None:
        # statement_cache_size=0 is required for Supabase Transaction Pooler (port 6543)
        # which does NOT support prepared statements. Safe for direct connections too.
        self._pool = await asyncpg.create_pool(
            self.connection_string,
            min_size=self.pool_min,
            max_size=self.pool_max,
            command_timeout=60,
            statement_cache_size=0,
        )
        logger.info("PostgreSQL connection pool initialized")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("PostgreSQL connection pool closed")

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Storage not initialized. Call initialize() first.")
        return self._pool

    # -- Document operations --

    async def save_document(
        self,
        document: Document,
        chunks: List[DocumentChunk],
    ) -> str:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO documents (title, source, content, doc_type, metadata)
                    VALUES ($1, $2, $3, $4, $5::jsonb)
                    RETURNING id
                    """,
                    _sanitize_text(document.title),
                    _sanitize_text(document.source),
                    _sanitize_text(document.content),
                    document.doc_type,
                    json.dumps(_sanitize_metadata(document.metadata)),
                )
                doc_id = str(row["id"])

                for chunk in chunks:
                    embedding_str = _embedding_to_pg(chunk.embedding) if chunk.has_embedding else None
                    await conn.execute(
                        """
                        INSERT INTO chunks (document_id, content, embedding, chunk_index, token_count, metadata)
                        VALUES ($1::uuid, $2, $3::vector, $4, $5, $6::jsonb)
                        """,
                        doc_id,
                        _sanitize_text(chunk.content),
                        embedding_str,
                        chunk.index,
                        chunk.token_count,
                        json.dumps(_sanitize_metadata(chunk.metadata)),
                    )

                logger.info("Saved document '%s' with %d chunks", document.title, len(chunks))
                return doc_id

    async def create_pending_document(
        self,
        title: str,
        source: str,
        doc_type: str = "unknown",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Insert a placeholder document with status='pending'.

        Used by background ingestion: the row exists immediately so the
        frontend can show it as 'processing', and is updated later when
        chunks are ready.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO documents (title, source, content, doc_type, metadata, status)
                VALUES ($1, $2, '', $3, $4::jsonb, 'pending')
                RETURNING id
                """,
                _sanitize_text(title),
                _sanitize_text(source),
                doc_type,
                json.dumps(_sanitize_metadata(metadata or {})),
            )
        doc_id = str(row["id"])
        logger.info("Created pending document '%s' (id=%s)", title, doc_id)
        return doc_id

    async def update_document_status(
        self,
        document_id: str,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        """Update only the status/error of an existing document."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE documents
                   SET status = $2,
                       ingestion_error = $3,
                       updated_at = NOW()
                 WHERE id = $1::uuid
                """,
                document_id,
                status,
                _sanitize_text(error),
            )
        logger.debug("Document %s status -> %s", document_id, status)

    async def complete_pending_document(
        self,
        document_id: str,
        content: str,
        chunks: List[DocumentChunk],
        metadata_update: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Fill in content + chunks for a pending document and mark completed."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Update document
                await conn.execute(
                    """
                    UPDATE documents
                       SET content = $2,
                           metadata = COALESCE(metadata, '{}'::jsonb) || $3::jsonb,
                           status = 'completed',
                           ingestion_error = NULL,
                           updated_at = NOW()
                     WHERE id = $1::uuid
                    """,
                    document_id,
                    _sanitize_text(content),
                    json.dumps(_sanitize_metadata(metadata_update or {})),
                )

                # Insert chunks
                for chunk in chunks:
                    embedding_str = (
                        _embedding_to_pg(chunk.embedding) if chunk.has_embedding else None
                    )
                    await conn.execute(
                        """
                        INSERT INTO chunks (document_id, content, embedding, chunk_index, token_count, metadata)
                        VALUES ($1::uuid, $2, $3::vector, $4, $5, $6::jsonb)
                        """,
                        document_id,
                        _sanitize_text(chunk.content),
                        embedding_str,
                        chunk.index,
                        chunk.token_count,
                        json.dumps(_sanitize_metadata(chunk.metadata)),
                    )

        logger.info("Completed document %s with %d chunks", document_id, len(chunks))

    async def get_document(self, document_id: str) -> Optional[Document]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, title, source, content, doc_type, metadata, created_at FROM documents WHERE id = $1::uuid",
                document_id,
            )
            if row is None:
                return None
            return Document(
                id=str(row["id"]),
                title=row["title"],
                source=row["source"],
                content=row["content"] or "",
                doc_type=row["doc_type"],
                metadata=json.loads(row["metadata"]) if row["metadata"] else {},
                created_at=row["created_at"],
            )

    async def get_document_by_title(self, title: str) -> Optional[Document]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, title, source, content, doc_type, metadata, created_at FROM documents WHERE title ILIKE $1 LIMIT 1",
                f"%{title}%",
            )
            if row is None:
                return None
            return Document(
                id=str(row["id"]),
                title=row["title"],
                source=row["source"],
                content=row["content"] or "",
                doc_type=row["doc_type"],
                metadata=json.loads(row["metadata"]) if row["metadata"] else {},
                created_at=row["created_at"],
            )

    async def list_documents(self) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT d.id, d.title, d.source, d.doc_type, d.status,
                       d.ingestion_error, d.created_at, d.updated_at,
                       COUNT(c.id) AS chunk_count
                FROM documents d
                LEFT JOIN chunks c ON c.document_id = d.id
                GROUP BY d.id
                ORDER BY d.created_at DESC
                """
            )
            return [
                {
                    "id": str(r["id"]),
                    "title": r["title"],
                    "source": r["source"],
                    "doc_type": r["doc_type"],
                    "status": r["status"] or "completed",
                    "ingestion_error": r["ingestion_error"],
                    "chunk_count": r["chunk_count"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                }
                for r in rows
            ]

    async def delete_document(self, document_id: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM documents WHERE id = $1::uuid", document_id
            )
            deleted = result.split()[-1] != "0"
            if deleted:
                logger.info("Deleted document %s", document_id)
            return deleted

    async def clear_all(self) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM conversation_chunks")
                await conn.execute("DELETE FROM conversations")
                await conn.execute("DELETE FROM chunks")
                await conn.execute("DELETE FROM documents")
            logger.info("Cleared all data")

    # -- Search operations --

    async def vector_search(
        self,
        query_embedding: List[float],
        limit: int = 10,
        similarity_threshold: Optional[float] = None,
    ) -> List[SearchResult]:
        threshold = similarity_threshold if similarity_threshold is not None else self.similarity_threshold
        embedding_str = _embedding_to_pg(query_embedding)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM match_chunks($1::vector, $2, $3)",
                embedding_str,
                limit,
                threshold,
            )

        return [
            SearchResult(
                chunk_id=str(r["id"]),
                document_id=str(r["document_id"]),
                content=r["content"],
                similarity=float(r["similarity"]),
                metadata=json.loads(r["metadata"]) if r["metadata"] else {},
                document_title=r["document_title"] or "",
                document_source=r["document_source"] or "",
            )
            for r in rows
        ]

    async def hybrid_search(
        self,
        query_embedding: List[float],
        query_text: str,
        limit: int = 10,
        text_weight: float = 0.3,
        similarity_threshold: Optional[float] = None,
    ) -> List[SearchResult]:
        # Use the configured similarity threshold (default low so we recall enough
        # candidates; the cross-encoder re-ranker handles precision after).
        threshold = (
            similarity_threshold
            if similarity_threshold is not None
            else self.similarity_threshold
        )
        embedding_str = _embedding_to_pg(query_embedding)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM hybrid_search($1::vector, $2, $3, $4, $5)",
                embedding_str,
                query_text,
                limit,
                text_weight,
                threshold,
            )

        return [
            SearchResult(
                chunk_id=str(r["chunk_id"]),
                document_id=str(r["document_id"]),
                content=r["content"],
                similarity=float(r["combined_score"]),
                metadata=json.loads(r["metadata"]) if r["metadata"] else {},
                document_title=r["document_title"] or "",
                document_source=r["document_source"] or "",
            )
            for r in rows
        ]

    # -- Chat history (lightweight, always-on persistence) --

    async def save_chat_message(
        self,
        session_id: str,
        user_message: str,
        assistant_message: str,
        intent: Optional[str] = None,
        sources_used: Optional[List[str]] = None,
        activity_metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Persist a chat exchange WITHOUT embedding it as RAG context.

        This is the lightweight equivalent of save_conversation: it only writes
        to the conversations table (for chat history retrieval via
        get_session_messages), skipping the expensive embedding + relevance
        scoring + summary that save_conversation does.

        Used to maintain in-session context across multiple user turns even
        when conversation_memory.save_to_rag is disabled.

        ``activity_metadata`` carries the per-turn timeline events
        (status/ingest/vigencia/jurisprudencia/sources/sourcerefs/clarify/
        case_shift/compact) plus case_index and turn_in_case so the
        Activity Panel can be rebuilt verbatim when the session is
        reopened. Optional and additive — old callers that omit it write
        the column's JSONB default '{}', so no existing query needs to
        change.
        """
        activity_payload = activity_metadata if activity_metadata is not None else {}
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO conversations
                    (session_id, user_message, assistant_message, intent, sources_used, metadata, activity_metadata)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb)
                RETURNING id
                """,
                session_id,
                _sanitize_text(user_message),
                _sanitize_text(assistant_message),
                intent,
                json.dumps(_sanitize_metadata(sources_used or [])),
                json.dumps({"chat_history_only": True}),
                json.dumps(_sanitize_metadata(activity_payload)),
            )
        return str(row["id"])

    # -- Conversation memory (heavyweight: embeddings + LLM eval) --

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
        user_message = _sanitize_text(user_message) or ""
        assistant_message = _sanitize_text(assistant_message) or ""
        combined_content = f"User: {user_message}\nAssistant: {assistant_message}"
        embedding_str = _embedding_to_pg(embedding)

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                conv_row = await conn.fetchrow(
                    """
                    INSERT INTO conversations
                        (session_id, user_message, assistant_message, intent, sources_used, relevance_score, metadata)
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7::jsonb)
                    RETURNING id
                    """,
                    session_id,
                    user_message,
                    assistant_message,
                    intent,
                    json.dumps(_sanitize_metadata(sources_used or [])),
                    relevance_score,
                    json.dumps(_sanitize_metadata(metadata or {})),
                )
                conv_id = str(conv_row["id"])

                await conn.execute(
                    """
                    INSERT INTO conversation_chunks
                        (conversation_id, content, embedding, session_id, metadata)
                    VALUES ($1::uuid, $2, $3::vector, $4, $5::jsonb)
                    """,
                    conv_id,
                    combined_content,
                    embedding_str,
                    session_id,
                    json.dumps(_sanitize_metadata({
                        "intent": intent,
                        "sources": sources_used or [],
                        **(metadata or {}),
                    })),
                )

        logger.debug("Saved conversation %s for session %s", conv_id, session_id)
        return conv_id

    async def search_conversations(
        self,
        query_embedding: List[float],
        limit: int = 5,
        similarity_threshold: Optional[float] = None,
        session_id: Optional[str] = None,
    ) -> List[SearchResult]:
        threshold = similarity_threshold if similarity_threshold is not None else self.similarity_threshold
        embedding_str = _embedding_to_pg(query_embedding)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM match_conversations($1::vector, $2, $3, $4)",
                embedding_str,
                limit,
                threshold,
                session_id,
            )

        return [
            SearchResult(
                chunk_id=str(r["id"]),
                document_id=str(r["conversation_id"]),
                content=r["content"],
                similarity=float(r["similarity"]),
                metadata=json.loads(r["metadata"]) if r["metadata"] else {},
                document_title=f"Conversation ({r['session_id']})",
                document_source="conversation_memory",
            )
            for r in rows
        ]

    async def search_all(
        self,
        query_embedding: List[float],
        doc_limit: int = 5,
        conv_limit: int = 3,
        similarity_threshold: Optional[float] = None,
    ) -> List[SearchResult]:
        threshold = similarity_threshold if similarity_threshold is not None else self.similarity_threshold
        embedding_str = _embedding_to_pg(query_embedding)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM match_all($1::vector, $2, $3, $4)",
                embedding_str,
                doc_limit,
                conv_limit,
                threshold,
            )

        return [
            SearchResult(
                chunk_id=str(r["id"]),
                document_id="",
                content=r["content"],
                similarity=float(r["similarity"]),
                metadata=json.loads(r["metadata"]) if r["metadata"] else {},
                document_title=r["source_name"] or "",
                document_source=r["source_type"] or "",
            )
            for r in rows
        ]

    # -- Session management (chat history) --

    async def list_sessions(self, limit: int = 100) -> List[Dict[str, Any]]:
        """List all chat sessions with title and message count."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    session_id,
                    COUNT(*)::int AS message_count,
                    MAX(created_at) AS last_message_at,
                    (ARRAY_AGG(user_message ORDER BY created_at ASC))[1] AS first_user_message
                FROM conversations
                GROUP BY session_id
                ORDER BY MAX(created_at) DESC
                LIMIT $1
                """,
                limit,
            )

        sessions = []
        for r in rows:
            title = (r["first_user_message"] or "Sin título")[:60]
            if len(r["first_user_message"] or "") > 60:
                title += "..."
            sessions.append({
                "session_id": r["session_id"],
                "title": title,
                "message_count": r["message_count"],
                "last_message_at": r["last_message_at"].isoformat() if r["last_message_at"] else None,
            })
        return sessions

    async def get_session_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """Retrieve all messages from a session in chronological order.

        Each assistant row carries `activity_metadata` so the frontend can
        rebuild the Activity Panel + case_shift/compact chat markers
        without re-running the agent. Pre-migration rows return `{}`.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, user_message, assistant_message, intent, sources_used,
                       activity_metadata, created_at
                FROM conversations
                WHERE session_id = $1
                ORDER BY created_at ASC
                """,
                session_id,
            )

        messages = []
        for r in rows:
            ts = r["created_at"].isoformat() if r["created_at"] else None
            sources = json.loads(r["sources_used"]) if r["sources_used"] else []
            activity = (
                json.loads(r["activity_metadata"]) if r["activity_metadata"] else {}
            )
            base_id = str(r["id"])
            messages.append({
                "id": f"{base_id}-u",
                "role": "user",
                "content": r["user_message"],
                "timestamp": ts,
                "sources": [],
            })
            messages.append({
                "id": f"{base_id}-a",
                "role": "assistant",
                "content": r["assistant_message"],
                "timestamp": ts,
                "sources": sources,
                "activity_metadata": activity,
            })
        return messages

    async def delete_session(self, session_id: str) -> bool:
        """Delete all conversations and embedded chunks for a session."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM conversation_chunks WHERE session_id = $1",
                    session_id,
                )
                result = await conn.execute(
                    "DELETE FROM conversations WHERE session_id = $1",
                    session_id,
                )
        deleted = result.split()[-1] != "0"
        if deleted:
            logger.info("Deleted session %s", session_id)
        return deleted

    # -- Case State --

    async def save_case_state(self, session_id: str, state: dict, turn_count: int) -> None:
        """Persist case state for a session (upsert)."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO case_states (session_id, state, turn_count, updated_at)
                   VALUES ($1, $2::jsonb, $3, NOW())
                   ON CONFLICT (session_id)
                   DO UPDATE SET state = $2::jsonb, turn_count = $3, updated_at = NOW()""",
                session_id, json.dumps(state), turn_count,
            )

    async def get_case_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Load case state for a session."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT state, turn_count FROM case_states WHERE session_id = $1",
                session_id,
            )
            if row:
                state = json.loads(row["state"]) if row["state"] else {}
                state["turn_count"] = row["turn_count"]
                return state
            return None

    # -- Chat Attachments --

    async def save_chat_attachment(
        self, session_id: str, doc_id: str, filename: str, chunk_count: int = 0
    ) -> None:
        """Record a file attachment for a chat session."""
        async with self.pool.acquire() as conn:
            # Get latest conversation row for this session
            row = await conn.fetchrow(
                "SELECT id, attachments FROM conversations WHERE session_id = $1 ORDER BY created_at DESC LIMIT 1",
                session_id,
            )
            if row:
                attachments = json.loads(row["attachments"]) if row["attachments"] else []
                attachments.append({
                    "doc_id": doc_id, "filename": filename,
                    "chunk_count": chunk_count, "status": "completed"
                })
                await conn.execute(
                    "UPDATE conversations SET attachments = $1::jsonb WHERE id = $2::uuid",
                    json.dumps(attachments), str(row["id"]),
                )

    async def get_session_attachments(self, session_id: str) -> List[Dict[str, Any]]:
        """Get all file attachments for a session."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT attachments FROM conversations WHERE session_id = $1 AND attachments != '[]'::jsonb ORDER BY created_at",
                session_id,
            )
            all_attachments = []
            for r in rows:
                atts = json.loads(r["attachments"]) if r["attachments"] else []
                all_attachments.extend(atts)
            return all_attachments

    async def get_document_chunks(self, document_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Get chunks for a specific document."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT content, chunk_index, metadata FROM chunks WHERE document_id = $1::uuid ORDER BY chunk_index LIMIT $2",
                document_id, limit,
            )
            return [{"content": r["content"], "index": r["chunk_index"],
                     "metadata": json.loads(r["metadata"]) if r["metadata"] else {}} for r in rows]

    # -- Legal: Normas search --

    async def search_normas(
        self,
        query_embedding: List[float],
        query_text: str = "",
        tipo: Optional[str] = None,
        estado: Optional[str] = None,
        sector: Optional[str] = None,
        limit: int = 10,
        text_weight: float = 0.4,
        similarity_threshold: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Search normas table using hybrid vector + text search."""
        threshold = similarity_threshold if similarity_threshold is not None else self.similarity_threshold
        embedding_str = _embedding_to_pg(query_embedding)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM search_normas($1::vector, $2, $3, $4, $5, $6, $7, $8)",
                embedding_str, query_text, tipo, estado, sector,
                limit, text_weight, threshold,
            )

        return [
            {
                "norma_id": str(r["norma_id"]),
                "tipo": r["tipo"],
                "numero": r["numero"],
                "anio": r["anio"],
                "titulo": r["titulo"],
                "estado": r["estado"],
                "fecha_expedicion": str(r["fecha_expedicion"]) if r["fecha_expedicion"] else None,
                "fuente_url": r["fuente_url"],
                "sector": r["sector"],
                "resumen": r["resumen"],
                "content_preview": r["content_preview"],
                "combined_score": float(r["combined_score"]),
                "vector_similarity": float(r["vector_similarity"]),
                "text_similarity": float(r["text_similarity"]),
            }
            for r in rows
        ]

    # -- Legal: Jurisprudencia search --

    async def search_jurisprudencia(
        self,
        query_embedding: List[float],
        query_text: str = "",
        corte: Optional[str] = None,
        tipo: Optional[str] = None,
        solo_precedentes: bool = False,
        limit: int = 10,
        text_weight: float = 0.4,
        similarity_threshold: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Search jurisprudencia table using hybrid vector + text search."""
        threshold = similarity_threshold if similarity_threshold is not None else self.similarity_threshold
        embedding_str = _embedding_to_pg(query_embedding)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM search_jurisprudencia($1::vector, $2, $3, $4, $5, $6, $7, $8)",
                embedding_str, query_text, corte, tipo, solo_precedentes,
                limit, text_weight, threshold,
            )

        return [
            {
                "sentencia_id": str(r["sentencia_id"]),
                "corte": r["corte"],
                "tipo_sentencia": r["tipo_sentencia"],
                "numero": r["numero"],
                "fecha": str(r["fecha"]) if r["fecha"] else None,
                "magistrado": r["magistrado"],
                "es_precedente": r["es_precedente"],
                "decision": r["decision"],
                "ratio_decidendi": r["ratio_decidendi"],
                "fuente_url": r["fuente_url"],
                "combined_score": float(r["combined_score"]),
                "vector_similarity": float(r["vector_similarity"]),
                "text_similarity": float(r["text_similarity"]),
            }
            for r in rows
        ]

    # -- Legal: Combined search (chunks + normas + jurisprudencia) --

    async def search_legal_all(
        self,
        query_embedding: List[float],
        query_text: str = "",
        chunk_limit: int = 10,
        norma_limit: int = 5,
        juris_limit: int = 5,
        similarity_threshold: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Unified search across RAG chunks, normas, and jurisprudencia."""
        threshold = similarity_threshold if similarity_threshold is not None else self.similarity_threshold
        embedding_str = _embedding_to_pg(query_embedding)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM search_legal_all($1::vector, $2, $3, $4, $5, $6)",
                embedding_str, query_text, chunk_limit, norma_limit,
                juris_limit, threshold,
            )

        return [
            {
                "result_id": str(r["result_id"]),
                "content": r["content"],
                "source_type": r["source_type"],
                "source_name": r["source_name"],
                "similarity": float(r["similarity"]),
                "estado": r["estado"],
                "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
            }
            for r in rows
        ]
