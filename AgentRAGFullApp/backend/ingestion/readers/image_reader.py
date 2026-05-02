"""Reader for image files using Vision LLM to generate text descriptions."""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff"}


def can_handle(file_path: str) -> bool:
    return Path(file_path).suffix.lower() in SUPPORTED_EXTENSIONS


def read(file_path: str) -> Tuple[str, None, dict]:
    """Use Vision LLM to describe image content as markdown text."""
    path = Path(file_path)
    metadata = {
        "reader": "image",
        "format": path.suffix.lower(),
        "file_name": path.name,
    }

    try:
        import openai

        with open(file_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")

        mime_map = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".bmp": "image/bmp",
            ".tiff": "image/tiff",
        }
        mime_type = mime_map.get(path.suffix.lower(), "image/png")

        client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Describe this image in detail for a knowledge base. "
                            "Include all visible text, data, diagrams, charts, or information. "
                            "Be thorough and structured."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{image_data}"},
                    },
                ],
            }],
            max_tokens=1000,
        )

        description = response.choices[0].message.content.strip()
        metadata["description_model"] = "gpt-4o-mini"

        md = f"# Image: {path.name}\n\n{description}\n"
        return md, None, metadata

    except ImportError:
        logger.warning("OpenAI not installed for image reading.")
        return f"[Image file: {path.name} - install openai for description]", None, metadata
    except Exception as e:
        logger.warning("Image description failed for %s: %s", path.name, e)
        return f"[Image file: {path.name} - could not generate description]", None, metadata
