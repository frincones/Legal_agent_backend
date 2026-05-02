"""Reader for source code files with language-aware metadata."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)

LANGUAGE_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".cpp": "cpp",
    ".c": "c",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
    ".r": "r",
    ".sql": "sql",
    ".sh": "shell",
    ".bash": "shell",
    ".ps1": "powershell",
    ".lua": "lua",
    ".dart": "dart",
    ".vue": "vue",
    ".svelte": "svelte",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".toml": "toml",
    ".tf": "terraform",
    ".proto": "protobuf",
    ".graphql": "graphql",
    ".gql": "graphql",
}

SUPPORTED_EXTENSIONS = set(LANGUAGE_MAP.keys())


def can_handle(file_path: str) -> bool:
    return Path(file_path).suffix.lower() in SUPPORTED_EXTENSIONS


def read(file_path: str) -> Tuple[str, None, dict]:
    """Read source code file and format as markdown with syntax highlighting."""
    path = Path(file_path)
    ext = path.suffix.lower()
    language = LANGUAGE_MAP.get(ext, "text")

    metadata = {
        "reader": "code",
        "format": ext,
        "language": language,
        "file_name": path.name,
    }

    content = ""
    for encoding in ("utf-8", "latin-1"):
        try:
            content = path.read_text(encoding=encoding)
            metadata["encoding"] = encoding
            break
        except UnicodeDecodeError:
            continue

    if not content:
        return f"[Could not read code file: {path.name}]", None, metadata

    metadata["line_count"] = content.count("\n") + 1

    # Format as markdown with language info for better chunking context
    markdown = f"# Source Code: {path.name}\n\n"
    markdown += f"**Language**: {language}\n"
    markdown += f"**File**: `{path.name}`\n\n"
    markdown += f"```{language}\n{content}\n```\n"

    return markdown, None, metadata
