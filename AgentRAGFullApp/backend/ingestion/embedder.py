"""Embedding generator with batch processing, retry logic, and caching."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from models.chunks import DocumentChunk
from utils.llm import get_openai_client

logger = logging.getLogger(__name__)


class EmbeddingCache:
    """Two-tier cache: in-memory LRU + persistent SQLite on disk.

    The on-disk layer survives backend restarts so re-uploading the same
    document or asking the same query returns instantly without paying the
    OpenAI embedding cost.
    """

    def __init__(self, max_size: int = 5000, persist_path: Optional[str] = None):
        self._cache: OrderedDict[str, List[float]] = OrderedDict()
        self._max_size = max_size

        # Persistence — defaults to backend/.cache/embeddings.db
        if persist_path is None:
            persist_path = os.getenv("EMBEDDING_CACHE_PATH", "")
            if not persist_path:
                root = Path(__file__).parent.parent
                persist_path = str(root / ".cache" / "embeddings.db")

        self._db_path = persist_path
        self._db_lock = threading.Lock()
        self._db_enabled = True
        try:
            Path(persist_path).parent.mkdir(parents=True, exist_ok=True)
            self._init_db()
            logger.info("Embedding disk cache: %s", persist_path)
        except Exception as e:
            logger.warning("Disk cache init failed: %s. In-memory only.", e)
            self._db_enabled = False

    def _init_db(self):
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS embeddings (
                    key TEXT PRIMARY KEY,
                    model TEXT NOT NULL,
                    vector TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_emb_model ON embeddings(model)")
            conn.commit()

    def _key(self, text: str, model: str = "") -> str:
        h = hashlib.md5((model + "|" + text).encode("utf-8", errors="replace")).hexdigest()
        return h

    def get(self, text: str, model: str = "") -> Optional[List[float]]:
        key = self._key(text, model)

        # L1: in-memory
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        # L2: disk
        if self._db_enabled:
            try:
                with self._db_lock, sqlite3.connect(self._db_path) as conn:
                    row = conn.execute(
                        "SELECT vector FROM embeddings WHERE key = ?",
                        (key,),
                    ).fetchone()
                if row:
                    embedding = json.loads(row[0])
                    # Promote to L1
                    self._cache[key] = embedding
                    self._cache.move_to_end(key)
                    if len(self._cache) > self._max_size:
                        self._cache.popitem(last=False)
                    return embedding
            except Exception as e:
                logger.debug("Disk cache read failed: %s", e)

        return None

    def put(self, text: str, embedding: List[float], model: str = "") -> None:
        key = self._key(text, model)

        # L1
        self._cache[key] = embedding
        self._cache.move_to_end(key)
        if len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

        # L2
        if self._db_enabled:
            try:
                import time
                with self._db_lock, sqlite3.connect(self._db_path) as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO embeddings (key, model, vector, created_at) VALUES (?, ?, ?, ?)",
                        (key, model, json.dumps(embedding), time.time()),
                    )
                    conn.commit()
            except Exception as e:
                logger.debug("Disk cache write failed: %s", e)

    def stats(self) -> Dict[str, int]:
        result = {"memory": len(self._cache), "disk": 0}
        if self._db_enabled:
            try:
                with self._db_lock, sqlite3.connect(self._db_path) as conn:
                    result["disk"] = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
            except Exception:
                pass
        return result


class EmbeddingGenerator:
    """Generate embeddings with batch processing, retry, and caching."""

    MODEL_CONFIGS = {
        "text-embedding-3-small": {"dimensions": 1536, "max_tokens": 8191},
        "text-embedding-3-large": {"dimensions": 3072, "max_tokens": 8191},
        "text-embedding-ada-002": {"dimensions": 1536, "max_tokens": 8191},
    }

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        batch_size: int = 100,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        use_cache: bool = True,
    ):
        self.model = model
        self.batch_size = batch_size
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.cache = EmbeddingCache() if use_cache else None

        config = self.MODEL_CONFIGS.get(model, {"dimensions": 1536, "max_tokens": 8191})
        self.dimensions = config["dimensions"]
        self.max_tokens = config["max_tokens"]
        # Conservative character cap: ~3.5 chars per token to stay safely under
        # the 8192-token limit (some languages produce more tokens per char).
        self.max_chars = int(self.max_tokens * 3.5)

    def _truncate(self, text: str) -> str:
        """Truncate text to safe character length to avoid embedding API errors."""
        if not text:
            return text
        # Strip NUL bytes defensively
        if "\x00" in text:
            text = text.replace("\x00", "")
        if len(text) > self.max_chars:
            logger.warning(
                "Truncating text from %d to %d chars for embedding",
                len(text), self.max_chars,
            )
            return text[:self.max_chars]
        return text

    async def generate_embedding(self, text: str, purpose: str = "embedding") -> List[float]:
        """Generate a single embedding with retry logic."""
        from utils.usage_tracker import tracker

        text = self._truncate(text)

        if self.cache:
            cached = self.cache.get(text, model=self.model)
            if cached is not None:
                return cached

        client = get_openai_client()

        for attempt in range(self.max_retries):
            try:
                response = await client.embeddings.create(
                    model=self.model,
                    input=text,
                )
                embedding = response.data[0].embedding

                if response.usage:
                    tracker.record_embedding(
                        model=self.model,
                        input_tokens=response.usage.prompt_tokens,
                        purpose=purpose,
                    )

                if self.cache:
                    self.cache.put(text, embedding, model=self.model)

                return embedding

            except Exception as e:
                if "rate_limit" in str(e).lower():
                    delay = self.retry_delay * (2 ** attempt)
                    logger.warning("Rate limited. Retrying in %.1fs (attempt %d/%d)", delay, attempt + 1, self.max_retries)
                    await asyncio.sleep(delay)
                elif attempt < self.max_retries - 1:
                    logger.warning("Embedding error: %s. Retrying (attempt %d/%d)", e, attempt + 1, self.max_retries)
                    await asyncio.sleep(self.retry_delay)
                else:
                    logger.error("Embedding failed after %d attempts: %s", self.max_retries, e)
                    return [0.0] * self.dimensions

        return [0.0] * self.dimensions

    async def generate_embeddings_batch(
        self,
        texts: List[str],
        purpose: str = "embedding_batch",
    ) -> List[List[float]]:
        """Generate embeddings for a batch of texts."""
        from utils.usage_tracker import tracker

        client = get_openai_client()

        # Truncate all texts to safe length first
        texts = [self._truncate(t) for t in texts]

        # Check cache first
        results: List[Optional[List[float]]] = [None] * len(texts)
        uncached_indices: List[int] = []
        uncached_texts: List[str] = []

        for i, text in enumerate(texts):
            if self.cache:
                cached = self.cache.get(text, model=self.model)
                if cached is not None:
                    results[i] = cached
                    continue
            uncached_indices.append(i)
            uncached_texts.append(text)

        if not uncached_texts:
            return results  # type: ignore

        for attempt in range(self.max_retries):
            try:
                response = await client.embeddings.create(
                    model=self.model,
                    input=uncached_texts,
                )

                if response.usage:
                    tracker.record_embedding(
                        model=self.model,
                        input_tokens=response.usage.prompt_tokens,
                        purpose=purpose,
                    )

                for j, item in enumerate(response.data):
                    idx = uncached_indices[j]
                    results[idx] = item.embedding
                    if self.cache:
                        self.cache.put(uncached_texts[j], item.embedding, model=self.model)

                return [r if r is not None else [0.0] * self.dimensions for r in results]

            except Exception as e:
                if attempt < self.max_retries - 1:
                    delay = self.retry_delay * (2 ** attempt)
                    logger.warning("Batch embedding error: %s. Retrying in %.1fs", e, delay)
                    await asyncio.sleep(delay)
                else:
                    logger.warning("Batch failed. Falling back to individual processing.")
                    return await self._process_individually(texts, results)

        return [r if r is not None else [0.0] * self.dimensions for r in results]

    async def _process_individually(
        self, texts: List[str], partial_results: List[Optional[List[float]]]
    ) -> List[List[float]]:
        """Fallback: process each text individually."""
        for i, text in enumerate(texts):
            if partial_results[i] is not None:
                continue
            partial_results[i] = await self.generate_embedding(text)
        return [r if r is not None else [0.0] * self.dimensions for r in partial_results]

    async def embed_chunks(
        self,
        chunks: List[DocumentChunk],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[DocumentChunk]:
        """Embed all chunks in batches."""
        total = len(chunks)

        for i in range(0, total, self.batch_size):
            batch = chunks[i:i + self.batch_size]
            texts = [c.content for c in batch]

            embeddings = await self.generate_embeddings_batch(texts)

            for j, embedding in enumerate(embeddings):
                chunks[i + j].embedding = embedding

            if progress_callback:
                progress_callback(min(i + len(batch), total), total)

            logger.debug("Embedded batch %d-%d / %d", i + 1, min(i + len(batch), total), total)

        embedded_count = sum(1 for c in chunks if c.has_embedding)
        logger.info("Embedded %d/%d chunks", embedded_count, total)
        return chunks


def create_embedder(
    model: str = "text-embedding-3-small",
    batch_size: int = 100,
    max_retries: int = 3,
    use_cache: bool = True,
) -> EmbeddingGenerator:
    """Factory function for creating an embedding generator."""
    return EmbeddingGenerator(
        model=model,
        batch_size=batch_size,
        max_retries=max_retries,
        use_cache=use_cache,
    )
