"""Main ingestion pipeline orchestrator."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, List, Optional

from config.schema import AppConfig
from models.chunks import ChunkingConfig, DocumentChunk
from models.documents import Document, IngestionResult
from ingestion.format_router import get_supported_extensions, read_file, chunk_content
from ingestion.metadata import extract_metadata
from ingestion.enrichment import enrich_chunks
from ingestion.embedder import create_embedder
from storage.base import BaseStorage

logger = logging.getLogger(__name__)


class IngestionPipeline:
    """Orchestrates the full document ingestion pipeline."""

    def __init__(self, config: AppConfig, storage: BaseStorage):
        self.config = config
        self.storage = storage
        self.embedder = create_embedder(
            model=config.ingestion.embedding.model,
            batch_size=config.ingestion.embedding.batch_size,
            max_retries=config.ingestion.embedding.max_retries,
            use_cache=config.ingestion.embedding.use_cache,
        )
        self.chunking_config = ChunkingConfig(
            chunk_size=config.ingestion.chunking.chunk_size,
            chunk_overlap=config.ingestion.chunking.chunk_overlap,
            max_tokens=config.ingestion.chunking.max_tokens,
            min_chunk_size=config.ingestion.chunking.min_chunk_size,
            use_semantic_splitting=config.ingestion.chunking.strategy != "simple",
            merge_peers=config.ingestion.chunking.merge_peers,
        )

    async def ingest_directory(
        self,
        directory: str,
        clean_before: bool = False,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
    ) -> IngestionResult:
        """Ingest all supported files from a directory."""
        start_time = time.time()
        result = IngestionResult()

        if clean_before:
            await self.storage.clear_all()
            logger.info("Cleared existing data")

        supported_ext = get_supported_extensions(self.config.ingestion.formats)
        files = self._find_files(directory, supported_ext)
        result.total_documents = len(files)

        logger.info("Found %d files to ingest in %s", len(files), directory)

        for i, file_path in enumerate(files):
            try:
                if progress_callback:
                    progress_callback(file_path, i + 1, len(files))

                doc_id = await self.ingest_file(str(file_path))

                if doc_id:
                    result.add_success(str(file_path), 0)
                    logger.info("[%d/%d] Ingested: %s", i + 1, len(files), file_path.name)
                else:
                    result.add_error(str(file_path), "Ingestion returned no document ID")

            except Exception as e:
                logger.error("[%d/%d] Failed: %s - %s", i + 1, len(files), file_path.name, e)
                result.add_error(str(file_path), str(e))

        result.duration_seconds = time.time() - start_time
        logger.info(
            "Ingestion complete: %d/%d successful, %d chunks, %.1fs",
            result.successful, result.total_documents,
            result.total_chunks, result.duration_seconds,
        )
        return result

    async def ingest_file(
        self,
        file_path: str,
        original_filename: Optional[str] = None,
    ) -> Optional[str]:
        """Ingest a single file through the full pipeline.

        Args:
            file_path: Path on disk (may be a temp file).
            original_filename: User-facing filename to use for title/source.
                Use this when file_path points to a temp file but you want to
                preserve the real name (e.g. uploads via API).
        """
        t0 = time.time()

        # Step 1: Read file
        content, docling_doc, reader_metadata = read_file(
            file_path, self.config.ingestion.formats
        )
        t_read = time.time()

        if not content or not content.strip():
            logger.warning("Empty content from %s", file_path)
            return None

        # Step 2: Extract metadata
        metadata = extract_metadata(content, file_path, reader_metadata)

        # Override file_name with the original (user-facing) name if provided.
        # This prevents temporary upload names like 'tmp7lfqdbwd.pdf' from
        # leaking into the document title and sources.
        if original_filename:
            metadata["file_name"] = original_filename
            metadata["original_filename"] = original_filename
            # Re-derive a clean title from the original name (without extension)
            metadata["title"] = Path(original_filename).stem

        title = metadata.get("title", Path(file_path).stem)
        source = metadata.get("file_name", file_path)

        # Step 3: Chunk content
        chunks = chunk_content(
            content=content,
            chunking_config=self.chunking_config,
            title=title,
            source=source,
            metadata=metadata,
            docling_doc=docling_doc,
        )
        t_chunk = time.time()

        if not chunks:
            logger.warning("No chunks generated from %s", file_path)
            return None

        # Step 4: Contextual enrichment (optional)
        if self.config.ingestion.enrichment.enabled:
            chunks = await enrich_chunks(
                chunks=chunks,
                document_content=content,
                title=title,
                source=source,
                model=self.config.ingestion.enrichment.model,
                max_concurrent=self.config.ingestion.enrichment.max_concurrent,
                max_document_chars=self.config.ingestion.enrichment.max_document_chars,
            )
        t_enrich = time.time()

        # Step 5: Generate embeddings
        chunks = await self.embedder.embed_chunks(chunks)
        t_embed = time.time()

        # Step 6: Save to storage
        document = Document(
            title=title,
            source=source,
            content=content,
            doc_type=metadata.get("reader", "unknown"),
            metadata=metadata,
        )

        doc_id = await self.storage.save_document(document, chunks)
        t_save = time.time()

        logger.info(
            "Ingested '%s' (%d chunks) in %.2fs total | "
            "read=%.2fs chunk=%.2fs enrich=%.2fs embed=%.2fs save=%.2fs",
            title, len(chunks), t_save - t0,
            t_read - t0, t_chunk - t_read, t_enrich - t_chunk,
            t_embed - t_enrich, t_save - t_embed,
        )
        return doc_id

    async def process_into_existing_document(
        self,
        document_id: str,
        file_path: str,
        original_filename: str,
    ) -> None:
        """Background-friendly variant of ingest_file that updates an
        existing 'pending' document row instead of creating a new one.

        Marks status='processing' on entry, then 'completed' or 'failed'.
        Used by the background ingestion endpoint.
        """
        t0 = time.time()
        try:
            await self.storage.update_document_status(document_id, "processing")

            # Step 1: Read file
            content, docling_doc, reader_metadata = read_file(
                file_path, self.config.ingestion.formats
            )
            t_read = time.time()

            if not content or not content.strip():
                await self.storage.update_document_status(
                    document_id, "failed",
                    error="Empty content from file (no extractable text).",
                )
                return

            # Step 2: Metadata
            metadata = extract_metadata(content, file_path, reader_metadata)
            metadata["file_name"] = original_filename
            metadata["original_filename"] = original_filename
            metadata["title"] = Path(original_filename).stem

            title = metadata["title"]
            source = original_filename

            # Step 3: Chunk
            chunks = chunk_content(
                content=content,
                chunking_config=self.chunking_config,
                title=title,
                source=source,
                metadata=metadata,
                docling_doc=docling_doc,
            )
            t_chunk = time.time()

            if not chunks:
                await self.storage.update_document_status(
                    document_id, "failed",
                    error="No chunks generated from content.",
                )
                return

            # Step 4: Enrichment (optional)
            if self.config.ingestion.enrichment.enabled:
                chunks = await enrich_chunks(
                    chunks=chunks,
                    document_content=content,
                    title=title,
                    source=source,
                    model=self.config.ingestion.enrichment.model,
                    max_concurrent=self.config.ingestion.enrichment.max_concurrent,
                    max_document_chars=self.config.ingestion.enrichment.max_document_chars,
                )
            t_enrich = time.time()

            # Step 5: Embeddings
            chunks = await self.embedder.embed_chunks(chunks)
            t_embed = time.time()

            # Step 6: Save chunks + complete document
            await self.storage.complete_pending_document(
                document_id=document_id,
                content=content,
                chunks=chunks,
                metadata_update=metadata,
            )
            t_save = time.time()

            logger.info(
                "[bg] Completed '%s' (%d chunks) in %.2fs | "
                "read=%.2fs chunk=%.2fs enrich=%.2fs embed=%.2fs save=%.2fs",
                title, len(chunks), t_save - t0,
                t_read - t0, t_chunk - t_read, t_enrich - t_chunk,
                t_embed - t_enrich, t_save - t_embed,
            )

        except Exception as e:
            logger.exception("[bg] Failed to ingest %s", original_filename)
            try:
                await self.storage.update_document_status(
                    document_id, "failed", error=f"{type(e).__name__}: {e}"
                )
            except Exception:
                pass

    async def ingest_text(
        self,
        text: str,
        title: str = "Untitled",
        source: str = "direct_input",
        doc_type: str = "text",
    ) -> Optional[str]:
        """Ingest raw text directly (not from file)."""
        if not text or not text.strip():
            return None

        metadata = {
            "reader": "direct",
            "format": "text",
            "title": title,
            "source": source,
        }

        chunks = chunk_content(
            content=text,
            chunking_config=self.chunking_config,
            title=title,
            source=source,
            metadata=metadata,
        )

        if not chunks:
            return None

        if self.config.ingestion.enrichment.enabled:
            chunks = await enrich_chunks(
                chunks=chunks,
                document_content=text,
                title=title,
                source=source,
                model=self.config.ingestion.enrichment.model,
                max_concurrent=self.config.ingestion.enrichment.max_concurrent,
                max_document_chars=self.config.ingestion.enrichment.max_document_chars,
            )

        chunks = await self.embedder.embed_chunks(chunks)

        document = Document(
            title=title,
            source=source,
            content=text,
            doc_type=doc_type,
            metadata=metadata,
        )

        return await self.storage.save_document(document, chunks)

    def _find_files(self, directory: str, extensions: set) -> List[Path]:
        """Find all supported files in a directory recursively."""
        dir_path = Path(directory)
        if not dir_path.exists():
            logger.error("Directory does not exist: %s", directory)
            return []

        files = []
        for ext in extensions:
            files.extend(dir_path.rglob(f"*{ext}"))

        # Sort by name for consistent ordering
        return sorted(set(files), key=lambda p: p.name.lower())
