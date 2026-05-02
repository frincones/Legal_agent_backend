"""Reader for plain text and markdown files."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".txt", ".md", ".markdown", ".rst", ".log"}


def can_handle(file_path: str) -> bool:
    return Path(file_path).suffix.lower() in SUPPORTED_EXTENSIONS


def read(file_path: str) -> Tuple[str, None, dict]:
    """Read a text/markdown file. Returns (content, None, metadata)."""
    path = Path(file_path)
    metadata = {
        "reader": "text",
        "format": path.suffix.lower(),
        "file_name": path.name,
    }

    content = ""
    for encoding in ("utf-8", "latin-1", "cp1252"):
        try:
            content = path.read_text(encoding=encoding)
            metadata["encoding"] = encoding
            break
        except UnicodeDecodeError:
            continue

    if not content:
        logger.warning("Could not read %s with any encoding", path.name)
        return f"[Could not read file: {path.name}]", None, metadata

    # Parse YAML frontmatter if present
    frontmatter = _parse_frontmatter(content)
    if frontmatter:
        metadata["frontmatter"] = frontmatter
        # Remove frontmatter from content
        content = re.sub(r"^---\s*\n.*?\n---\s*\n", "", content, count=1, flags=re.DOTALL)

    metadata["word_count"] = len(content.split())
    metadata["line_count"] = content.count("\n") + 1

    return content, None, metadata


def _parse_frontmatter(content: str) -> Optional[dict]:
    """Extract YAML frontmatter from markdown files."""
    if not content.startswith("---"):
        return None
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return None
    try:
        import yaml
        return yaml.safe_load(match.group(1))
    except Exception:
        return None
