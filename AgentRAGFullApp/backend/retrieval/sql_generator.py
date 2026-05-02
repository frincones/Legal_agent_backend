"""SQL generator: converts natural language to safe SELECT queries."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from storage.base import BaseStorage
from utils.llm import llm_generate

logger = logging.getLogger(__name__)

SQL_PROMPT = """You are a SQL query generator for PostgreSQL. Generate a safe, read-only SQL query based on the user's question.

Available tables and their columns:
{tables_schema}

RULES:
- ONLY generate SELECT queries. NEVER use INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE.
- Always include a LIMIT clause (max 50 rows).
- Use proper column names exactly as shown above.
- If the question cannot be answered with the available tables, respond with "CANNOT_QUERY".
- Respond with ONLY the SQL query, no explanation.

User question: "{question}"

SQL:"""


async def generate_and_execute(
    question: str,
    storage: BaseStorage,
    tables_schema: str,
    model: str = "gpt-4o-mini",
) -> Optional[List[Dict[str, Any]]]:
    """Generate a SQL query from natural language and execute it safely."""
    try:
        sql = await llm_generate(
            prompt=SQL_PROMPT.format(
                tables_schema=tables_schema,
                question=question[:500],
            ),
            model=model,
            temperature=0.0,
            max_tokens=300,
            purpose="sql_generation",
        )

        sql = sql.strip().strip("`").strip()
        if sql.startswith("sql"):
            sql = sql[3:].strip()

        # Safety check
        if not _is_safe_query(sql):
            logger.warning("Unsafe SQL rejected: %s", sql[:100])
            return None

        if sql == "CANNOT_QUERY":
            logger.debug("SQL generator: cannot answer question with available tables")
            return None

        logger.debug("Generated SQL: %s", sql)

        # Execute via storage pool
        from storage.postgres import PostgresStorage
        if isinstance(storage, PostgresStorage):
            async with storage.pool.acquire() as conn:
                rows = await conn.fetch(sql)
                return [dict(row) for row in rows]

        return None

    except Exception as e:
        logger.warning("SQL generation/execution failed: %s", e)
        return None


def _is_safe_query(sql: str) -> bool:
    """Validate that the query is a safe read-only SELECT."""
    sql_upper = sql.upper().strip()

    # Must start with SELECT or WITH (for CTEs)
    if not sql_upper.startswith(("SELECT", "WITH")):
        return False

    # Block dangerous keywords
    dangerous = {"INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "GRANT", "REVOKE", "EXEC", "EXECUTE"}
    words = set(sql_upper.split())
    if words & dangerous:
        return False

    # Block semicolons (prevent injection of second query)
    if ";" in sql and sql.strip().rstrip(";").count(";") > 0:
        return False

    return True
