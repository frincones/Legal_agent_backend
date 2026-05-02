"""Metadata extraction from document content."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


def extract_metadata(
    content: str,
    file_path: str,
    reader_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Extract comprehensive metadata from document content and file info."""
    path = Path(file_path)
    metadata = reader_metadata.copy() if reader_metadata else {}

    # File-level metadata
    metadata["file_path"] = str(path)
    metadata["file_name"] = path.name
    metadata["file_extension"] = path.suffix.lower()
    metadata["ingested_at"] = datetime.now().isoformat()

    # Content-level metadata
    if content:
        metadata.setdefault("word_count", len(content.split()))
        metadata.setdefault("line_count", content.count("\n") + 1)
        metadata["char_count"] = len(content)

    # Try to extract title from content
    if "title" not in metadata or not metadata["title"]:
        metadata["title"] = _extract_title(content, path.stem)

    # Detect language for non-English content
    metadata.setdefault("language", _detect_language_hint(content))

    # File stats if available
    try:
        stat = path.stat()
        metadata["file_size_bytes"] = stat.st_size
        metadata["file_modified"] = datetime.fromtimestamp(stat.st_mtime).isoformat()
    except (OSError, ValueError):
        pass

    return metadata


def _extract_title(content: str, fallback: str) -> str:
    """Try to extract a title from markdown headings or first line."""
    if not content:
        return fallback

    # Look for # heading
    match = re.match(r"^#\s+(.+)$", content.strip(), re.MULTILINE)
    if match:
        return match.group(1).strip()

    # Use first non-empty line as title
    for line in content.split("\n"):
        line = line.strip()
        if line and not line.startswith("```") and not line.startswith("---"):
            return line[:100]

    return fallback


def _detect_language_hint(content: str) -> str:
    """Simple language detection based on common words."""
    if not content:
        return "unknown"

    sample = content[:1000].lower()
    es_words = {"el", "la", "los", "las", "de", "en", "por", "para", "con", "que", "es", "un", "una"}
    en_words = {"the", "is", "are", "was", "were", "have", "has", "with", "for", "and", "this", "that"}

    words = set(re.findall(r"\b\w+\b", sample))
    es_count = len(words & es_words)
    en_count = len(words & en_words)

    if es_count > en_count and es_count >= 3:
        return "es"
    elif en_count >= 2:
        return "en"
    return "unknown"
