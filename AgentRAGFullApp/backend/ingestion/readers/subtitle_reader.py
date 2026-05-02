"""Reader for subtitle files: SRT, VTT."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".srt", ".vtt", ".sub"}


def can_handle(file_path: str) -> bool:
    return Path(file_path).suffix.lower() in SUPPORTED_EXTENSIONS


def read(file_path: str) -> Tuple[str, None, dict]:
    """Parse subtitle file into structured markdown."""
    path = Path(file_path)
    ext = path.suffix.lower()
    metadata = {
        "reader": "subtitle",
        "format": ext,
        "file_name": path.name,
    }

    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = path.read_text(encoding="latin-1")

    if ext == ".srt":
        text = _parse_srt(content)
    elif ext == ".vtt":
        text = _parse_vtt(content)
    else:
        text = content

    metadata["word_count"] = len(text.split())

    md = f"# Transcript: {path.name}\n\n{text}\n"
    return md, None, metadata


def _parse_srt(content: str) -> str:
    """Parse SRT subtitle format into clean text with timestamps."""
    blocks = re.split(r"\n\n+", content.strip())
    lines = []
    for block in blocks:
        parts = block.strip().split("\n")
        if len(parts) >= 3:
            timestamp = parts[1].strip()
            text = " ".join(p.strip() for p in parts[2:])
            # Clean HTML tags
            text = re.sub(r"<[^>]+>", "", text)
            if text:
                time_match = re.match(r"(\d{2}:\d{2}:\d{2})", timestamp)
                time_str = time_match.group(1) if time_match else ""
                lines.append(f"[{time_str}] {text}")

    return "\n".join(lines)


def _parse_vtt(content: str) -> str:
    """Parse WebVTT subtitle format into clean text with timestamps."""
    # Remove WEBVTT header
    content = re.sub(r"^WEBVTT.*?\n\n", "", content, flags=re.DOTALL)

    blocks = re.split(r"\n\n+", content.strip())
    lines = []
    for block in blocks:
        parts = block.strip().split("\n")
        for i, part in enumerate(parts):
            if "-->" in part:
                timestamp = part.strip()
                text = " ".join(p.strip() for p in parts[i + 1:])
                text = re.sub(r"<[^>]+>", "", text)
                if text:
                    time_match = re.match(r"(\d{2}:\d{2}[:\.]?\d{0,2})", timestamp)
                    time_str = time_match.group(1) if time_match else ""
                    lines.append(f"[{time_str}] {text}")
                break

    return "\n".join(lines)
