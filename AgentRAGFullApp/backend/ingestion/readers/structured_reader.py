"""Reader for structured data files: CSV, JSON, XML, YAML."""

from __future__ import annotations

import csv
import io
import json
import logging
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".csv", ".json", ".jsonl", ".xml", ".yaml", ".yml"}


def can_handle(file_path: str) -> bool:
    return Path(file_path).suffix.lower() in SUPPORTED_EXTENSIONS


def read(file_path: str) -> Tuple[str, None, dict]:
    """Read structured data and convert to markdown."""
    path = Path(file_path)
    ext = path.suffix.lower()
    metadata = {
        "reader": "structured",
        "format": ext,
        "file_name": path.name,
    }

    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raw = path.read_text(encoding="latin-1")

    if ext == ".csv":
        return _read_csv(raw, path.name, metadata)
    elif ext == ".json":
        return _read_json(raw, path.name, metadata)
    elif ext == ".jsonl":
        return _read_jsonl(raw, path.name, metadata)
    elif ext == ".xml":
        return _read_xml(raw, path.name, metadata)
    elif ext in (".yaml", ".yml"):
        return _read_yaml(raw, path.name, metadata)
    else:
        return raw, None, metadata


def _read_csv(raw: str, name: str, metadata: dict) -> Tuple[str, None, dict]:
    reader = csv.DictReader(io.StringIO(raw))
    rows = list(reader)
    metadata["row_count"] = len(rows)
    metadata["columns"] = reader.fieldnames or []

    md = f"# Data: {name}\n\n"
    md += f"**Rows**: {len(rows)} | **Columns**: {', '.join(metadata['columns'])}\n\n"

    if rows:
        # Create markdown table
        headers = metadata["columns"]
        md += "| " + " | ".join(headers) + " |\n"
        md += "| " + " | ".join(["---"] * len(headers)) + " |\n"
        for row in rows:
            md += "| " + " | ".join(str(row.get(h, "")) for h in headers) + " |\n"

    return md, None, metadata


def _read_json(raw: str, name: str, metadata: dict) -> Tuple[str, None, dict]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("Invalid JSON in %s: %s", name, e)
        return raw, None, metadata

    if isinstance(data, list):
        metadata["record_count"] = len(data)
    elif isinstance(data, dict):
        metadata["keys"] = list(data.keys())

    md = f"# Data: {name}\n\n"
    md += f"```json\n{json.dumps(data, indent=2, ensure_ascii=False)}\n```\n"
    return md, None, metadata


def _read_jsonl(raw: str, name: str, metadata: dict) -> Tuple[str, None, dict]:
    lines = [l for l in raw.strip().split("\n") if l.strip()]
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    metadata["record_count"] = len(records)

    md = f"# Data: {name}\n\n**Records**: {len(records)}\n\n"
    md += f"```json\n{json.dumps(records, indent=2, ensure_ascii=False)}\n```\n"
    return md, None, metadata


def _read_xml(raw: str, name: str, metadata: dict) -> Tuple[str, None, dict]:
    md = f"# Data: {name}\n\n"
    md += f"```xml\n{raw}\n```\n"
    return md, None, metadata


def _read_yaml(raw: str, name: str, metadata: dict) -> Tuple[str, None, dict]:
    md = f"# Data: {name}\n\n"
    md += f"```yaml\n{raw}\n```\n"
    return md, None, metadata
