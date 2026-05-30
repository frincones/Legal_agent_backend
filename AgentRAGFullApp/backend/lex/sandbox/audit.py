"""Sprint M20.12 · Audit persist para sandbox_execution_log."""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional
from uuid import UUID

logger = logging.getLogger(__name__)


def _sha256_hex(s: str | bytes | None) -> Optional[str]:
    if s is None:
        return None
    if isinstance(s, str):
        s = s.encode("utf-8")
    return hashlib.sha256(s).hexdigest()[:32]


async def persist_sandbox_execution(
    pool,
    *,
    generation_id: UUID | str,
    firm_id: Optional[UUID | str] = None,
    user_id: Optional[UUID | str] = None,
    tool_name: Optional[str] = None,
    code: str = "",
    input_data: Optional[dict] = None,
    result: Any = None,
) -> Optional[str]:
    """INSERT en sandbox_execution_log. Best-effort (no rompe si falla)."""
    if pool is None or result is None:
        return None

    code_hash = _sha256_hex(code)
    code_preview = (code or "")[:500]
    input_hash = _sha256_hex(json.dumps(input_data or {}, sort_keys=True, default=str))

    stdout_preview = (result.stdout or "")[:1000]
    stderr_preview = (result.stderr or "")[:1000]

    files = result.files_created or []
    if not isinstance(files, list):
        files = [str(files)]

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                insert into sandbox_execution_log (
                  generation_id, firm_id, user_id, tool_name,
                  duration_ms, exit_code, success, error_class, error_message,
                  backend_used, code_hash, code_preview, input_hash,
                  stdout_preview, stderr_preview, files_created,
                  bytes_read_network, timeout_s
                ) values (
                  $1::uuid, $2, $3, $4,
                  $5, $6, $7, $8, $9,
                  $10, $11, $12, $13,
                  $14, $15, $16::jsonb,
                  $17, $18
                ) returning id
                """,
                str(generation_id),
                firm_id, user_id, tool_name,
                result.duration_ms, result.exit_code, result.success,
                None,  # error_class — el SandboxResult.error es string genérico
                result.error,
                result.backend_used,
                code_hash, code_preview, input_hash,
                stdout_preview, stderr_preview,
                json.dumps(files, default=str),
                int(result.bytes_read_network or 0),
                None,  # timeout_s no expuesto en SandboxResult
            )
            return str(row["id"]) if row else None
    except Exception as e:
        logger.warning("sandbox_execution_log persist failed: %s", e)
        return None
