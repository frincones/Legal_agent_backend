"""Tool 2 · load_playbook · firm_playbook como CLAUDE.md equivalente."""
from __future__ import annotations

import json
import logging
from typing import Any

from .base import ToolContext, ToolDef

logger = logging.getLogger(__name__)


_DEFAULT_PLAYBOOK = {
    "jurisdiction_default": "CO",
    "redline_style": "tracked",
    "tone": "formal",
    "preferred_clauses": {},
    "forbidden_terms": [],
    "required_clauses": [],
    "escalation_matrix": [],
    "raw_md": (
        "# Playbook por defecto (LexAI)\n\n"
        "Jurisdicción: Colombia · Tono: formal · Estilo: tracked changes.\n"
        "Esta firma no tiene playbook personalizado todavía. "
        "Configúralo en /settings/playbook (M20.12)."
    ),
    "is_default": True,
}


class LoadPlaybookTool(ToolDef):
    name = "load_playbook"
    description = (
        "Carga el playbook (CLAUDE.md) de la firma actual: jurisdicción, tono, "
        "cláusulas preferidas, términos prohibidos y reglas de escalación. "
        "Llamar SIEMPRE al inicio de cualquier generación; el resultado debe "
        "guiar el estilo y las decisiones del redactado. Si no hay playbook "
        "configurado, retorna defaults seguros."
    )
    input_schema = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    cacheable = True
    cache_ttl_seconds = 600
    timeout_seconds = 5.0

    def __init__(self, pool=None, **_: Any):
        self.pool = pool

    async def run(self, ctx: ToolContext) -> dict:
        pool = self.pool or ctx.pool
        firm_id = ctx.firm_id
        if pool is None or firm_id is None:
            return _DEFAULT_PLAYBOOK.copy()

        sql = """
            select jurisdiction_default, redline_style, tone, preferred_clauses,
                   forbidden_terms, required_clauses, escalation_matrix, raw_md,
                   version, updated_at
            from firm_playbook
            where firm_id = $1
            limit 1
        """
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(sql, firm_id)
        except Exception as e:
            logger.warning("load_playbook query failed: %s", e)
            return _DEFAULT_PLAYBOOK.copy()

        if not row:
            return _DEFAULT_PLAYBOOK.copy()

        return {
            "jurisdiction_default": row["jurisdiction_default"] or "CO",
            "redline_style": row["redline_style"] or "tracked",
            "tone": row["tone"] or "formal",
            "preferred_clauses": _coerce_jsonb(row["preferred_clauses"]),
            "forbidden_terms": list(row["forbidden_terms"] or []),
            "required_clauses": list(row["required_clauses"] or []),
            "escalation_matrix": _coerce_jsonb(row["escalation_matrix"]),
            "raw_md": row["raw_md"] or "",
            "version": row["version"],
            "updated_at": str(row["updated_at"]) if row["updated_at"] else None,
            "is_default": False,
        }


def _coerce_jsonb(val: Any) -> Any:
    if val is None:
        return {} if isinstance(val, (dict, type(None))) else []
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return val
    return val


def build_tool(pool=None, **_: Any) -> ToolDef:
    return LoadPlaybookTool(pool=pool)
