"""Format router: detects file type and delegates to the correct reader and chunker."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Tuple

from config.schema import FormatSettings, ChunkingSettings
from models.chunks import ChunkingConfig, DocumentChunk
from ingestion.readers import (
    docling_reader,
    text_reader,
    code_reader,
    structured_reader,
    audio_reader,
    subtitle_reader,
    image_reader,
)
from ingestion.chunkers import (
    hybrid_chunker,
    code_chunker,
    semantic_chunker,
    record_chunker,
    simple_chunker,
)

logger = logging.getLogger(__name__)

# Reader priority order
_READERS = [
    ("documents", docling_reader),
    ("audio", audio_reader),
    ("subtitles", subtitle_reader),
    ("images", image_reader),
    ("code", code_reader),
    ("structured", structured_reader),
    ("text", text_reader),
]


def get_supported_extensions(formats: FormatSettings) -> set:
    """Return all file extensions enabled by current format settings."""
    extensions = set()
    if formats.documents:
        extensions.update(docling_reader.SUPPORTED_EXTENSIONS)
    if formats.text:
        extensions.update(text_reader.SUPPORTED_EXTENSIONS)
    if formats.code:
        extensions.update(code_reader.SUPPORTED_EXTENSIONS)
    if formats.structured:
        extensions.update(structured_reader.SUPPORTED_EXTENSIONS)
    if formats.audio:
        extensions.update(audio_reader.SUPPORTED_EXTENSIONS)
    if formats.subtitles:
        extensions.update(subtitle_reader.SUPPORTED_EXTENSIONS)
    if formats.images:
        extensions.update(image_reader.SUPPORTED_EXTENSIONS)
    return extensions


def read_file(
    file_path: str,
    formats: FormatSettings,
) -> Tuple[str, Optional[object], dict]:
    """
    Read a file using the appropriate reader based on extension and config.
    Returns (markdown_content, docling_document_or_none, metadata).
    """
    ext = Path(file_path).suffix.lower()

    format_reader_map = {
        "documents": docling_reader,
        "audio": audio_reader,
        "subtitles": subtitle_reader,
        "images": image_reader,
        "code": code_reader,
        "structured": structured_reader,
        "text": text_reader,
    }

    for format_key, reader in _READERS:
        format_enabled = getattr(formats, format_key, False)
        if format_enabled and reader.can_handle(file_path):
            logger.info("Reading '%s' with %s reader", Path(file_path).name, format_key)
            return reader.read(file_path)

    logger.warning("No reader found for '%s' (ext=%s). Trying as plain text.", file_path, ext)
    return text_reader.read(file_path)


def chunk_content(
    content: str,
    chunking_config: ChunkingConfig,
    title: str = "",
    source: str = "",
    metadata: Optional[dict] = None,
    docling_doc: Optional[object] = None,
) -> List[DocumentChunk]:
    """
    Chunk content using the appropriate strategy based on config and content type.
    """
    strategy = (metadata or {}).get("chunk_strategy", chunking_config.use_semantic_splitting and "auto" or "simple")
    reader_type = (metadata or {}).get("reader", "")
    fmt = (metadata or {}).get("format", "")

    if strategy == "auto" or chunking_config.use_semantic_splitting:
        # Auto-select chunker based on content type
        if reader_type == "code":
            return code_chunker.chunk(content, chunking_config, title, source, metadata)
        elif reader_type == "structured" and fmt in (".csv", ".json", ".jsonl"):
            return record_chunker.chunk(content, chunking_config, title, source, metadata)
        elif docling_doc is not None:
            return hybrid_chunker.chunk(content, chunking_config, title, source, metadata, docling_doc)
        elif reader_type in ("text", "subtitle", "audio", "image"):
            return semantic_chunker.chunk(content, chunking_config, title, source, metadata)
        else:
            return simple_chunker.chunk(content, chunking_config, title, source, metadata)
    else:
        return simple_chunker.chunk(content, chunking_config, title, source, metadata)
