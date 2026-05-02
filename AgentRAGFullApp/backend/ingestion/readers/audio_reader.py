"""Reader for audio files using Whisper ASR via Docling."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".wma", ".aac"}


def can_handle(file_path: str) -> bool:
    return Path(file_path).suffix.lower() in SUPPORTED_EXTENSIONS


def read(file_path: str) -> Tuple[str, None, dict]:
    """Transcribe audio file using Whisper ASR. Returns (markdown_text, None, metadata)."""
    path = Path(file_path)
    metadata = {
        "reader": "audio",
        "format": path.suffix.lower(),
        "file_name": path.name,
    }

    try:
        from docling.document_converter import DocumentConverter
        from docling.datamodel.pipeline_options import PipelineOptions

        pipeline_options = PipelineOptions()
        converter = DocumentConverter(pipeline_options=pipeline_options)
        result = converter.convert(str(path))
        content = result.document.export_to_markdown()

        if not content or not content.strip():
            return f"[Audio transcription returned empty for: {path.name}]", None, metadata

        metadata["transcription"] = "whisper"
        md = f"# Audio Transcription: {path.name}\n\n{content}\n"
        return md, None, metadata

    except ImportError:
        logger.warning("Docling with ASR not available. Install: pip install docling[asr]")
        return _fallback_whisper(file_path, metadata)
    except Exception as e:
        logger.warning("Docling ASR failed for %s: %s. Trying fallback.", path.name, e)
        return _fallback_whisper(file_path, metadata)


def _fallback_whisper(file_path: str, metadata: dict) -> Tuple[str, None, dict]:
    """Fallback: try OpenAI Whisper API."""
    try:
        import openai
        import os

        client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        with open(file_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="text",
            )

        metadata["transcription"] = "openai_whisper"
        md = f"# Audio Transcription: {Path(file_path).name}\n\n{transcript}\n"
        return md, None, metadata

    except Exception as e:
        logger.error("All audio transcription methods failed for %s: %s", file_path, e)
        return f"[Could not transcribe audio: {Path(file_path).name}]", None, metadata
