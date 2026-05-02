"""Record-based chunker for structured data (CSV rows, JSON records)."""

from __future__ import annotations

import json
import logging
import re
from typing import Dict, List, Optional

from models.chunks import ChunkingConfig, DocumentChunk

logger = logging.getLogger(__name__)


def chunk(
    content: str,
    config: ChunkingConfig,
    title: str = "",
    source: str = "",
    metadata: Optional[Dict] = None,
) -> List[DocumentChunk]:
    """Split structured data into record-based chunks."""
    if not content or not content.strip():
        return []

    base_metadata = {"title": title, "source": source, "chunk_method": "record"}
    if metadata:
        base_metadata.update(metadata)

    fmt = (metadata or {}).get("format", "")

    if fmt in (".csv",):
        return _chunk_table(content, config, base_metadata)
    elif fmt in (".json", ".jsonl"):
        return _chunk_json(content, config, base_metadata)
    else:
        return _chunk_table(content, config, base_metadata)


def _chunk_table(content: str, config: ChunkingConfig, base_metadata: dict) -> List[DocumentChunk]:
    """Chunk markdown tables by groups of rows, preserving header."""
    lines = content.split("\n")

    # Find the markdown table
    header_line = ""
    separator_line = ""
    data_lines: List[str] = []
    preamble_lines: List[str] = []
    in_table = False

    for line in lines:
        if re.match(r"^\|.*\|$", line.strip()):
            if not in_table:
                header_line = line
                in_table = True
            elif re.match(r"^\|[\s\-|]+\|$", line.strip()):
                separator_line = line
            else:
                data_lines.append(line)
        else:
            if not in_table:
                preamble_lines.append(line)

    if not data_lines:
        # Not a table format, fall back to paragraph chunking
        from ingestion.chunkers import simple_chunker
        return simple_chunker.chunk(content, config,
                                     base_metadata.get("title", ""),
                                     base_metadata.get("source", ""),
                                     base_metadata)

    preamble = "\n".join(preamble_lines).strip()
    table_header = f"{header_line}\n{separator_line}" if separator_line else header_line

    # Group rows into chunks that fit within chunk_size
    chunks: List[DocumentChunk] = []
    current_rows: List[str] = []
    current_size = len(preamble) + len(table_header) + 10

    for row in data_lines:
        if current_size + len(row) + 1 > config.chunk_size and current_rows:
            chunk_text = preamble + "\n\n" + table_header + "\n" + "\n".join(current_rows)
            chunks.append(DocumentChunk(
                content=chunk_text.strip(),
                index=len(chunks),
                metadata={**base_metadata},
            ))
            current_rows = [row]
            current_size = len(preamble) + len(table_header) + len(row) + 10
        else:
            current_rows.append(row)
            current_size += len(row) + 1

    if current_rows:
        chunk_text = preamble + "\n\n" + table_header + "\n" + "\n".join(current_rows)
        chunks.append(DocumentChunk(
            content=chunk_text.strip(),
            index=len(chunks),
            metadata={**base_metadata},
        ))

    for c in chunks:
        c.metadata["total_chunks"] = len(chunks)

    logger.debug("RecordChunker (table): %d chunks", len(chunks))
    return chunks


def _chunk_json(content: str, config: ChunkingConfig, base_metadata: dict) -> List[DocumentChunk]:
    """Chunk JSON arrays by groups of records."""
    # Try to extract JSON from markdown code blocks
    json_match = re.search(r"```json\s*\n(.*?)\n```", content, re.DOTALL)
    raw_json = json_match.group(1) if json_match else content

    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        from ingestion.chunkers import simple_chunker
        return simple_chunker.chunk(content, config,
                                     base_metadata.get("title", ""),
                                     base_metadata.get("source", ""),
                                     base_metadata)

    if not isinstance(data, list):
        # Single object, return as one chunk
        return [DocumentChunk(
            content=content,
            index=0,
            metadata={**base_metadata, "total_chunks": 1},
        )]

    # Group records into chunks
    chunks: List[DocumentChunk] = []
    current_records: List[dict] = []
    current_size = 0
    title = base_metadata.get("title", "Data")

    for record in data:
        record_str = json.dumps(record, indent=2, ensure_ascii=False)
        if current_size + len(record_str) > config.chunk_size and current_records:
            chunk_text = f"# {title} (records {len(chunks) * len(current_records) + 1}-{(len(chunks) + 1) * len(current_records)})\n\n"
            chunk_text += "```json\n" + json.dumps(current_records, indent=2, ensure_ascii=False) + "\n```"
            chunks.append(DocumentChunk(
                content=chunk_text,
                index=len(chunks),
                metadata={**base_metadata, "record_count": len(current_records)},
            ))
            current_records = [record]
            current_size = len(record_str)
        else:
            current_records.append(record)
            current_size += len(record_str)

    if current_records:
        chunk_text = f"# {title}\n\n"
        chunk_text += "```json\n" + json.dumps(current_records, indent=2, ensure_ascii=False) + "\n```"
        chunks.append(DocumentChunk(
            content=chunk_text,
            index=len(chunks),
            metadata={**base_metadata, "record_count": len(current_records)},
        ))

    for c in chunks:
        c.metadata["total_chunks"] = len(chunks)

    logger.debug("RecordChunker (json): %d chunks", len(chunks))
    return chunks
