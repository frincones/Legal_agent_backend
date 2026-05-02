"""Database tools: structured data queries for the agent."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from storage.base import BaseStorage
from retrieval.sql_generator import generate_and_execute

logger = logging.getLogger(__name__)


async def query_database(
    question: str,
    storage: BaseStorage,
    tables_schema: str,
    model: str = "gpt-4o-mini",
) -> str:
    """Query structured data using natural language."""
    results = await generate_and_execute(
        question=question,
        storage=storage,
        tables_schema=tables_schema,
        model=model,
    )

    if results is None:
        return "Could not generate a query for this question with the available tables."

    if not results:
        return "Query executed successfully but returned no results."

    # Format results as readable text
    formatted = f"**Query Results** ({len(results)} rows):\n\n"

    if len(results) <= 10:
        for i, row in enumerate(results, 1):
            row_str = ", ".join(f"{k}: {v}" for k, v in row.items())
            formatted += f"{i}. {row_str}\n"
    else:
        # For larger results, use a summary
        formatted += f"First 5 rows:\n"
        for i, row in enumerate(results[:5], 1):
            row_str = ", ".join(f"{k}: {v}" for k, v in row.items())
            formatted += f"{i}. {row_str}\n"
        formatted += f"\n... and {len(results) - 5} more rows."

    return formatted
