"""Sprint E · Playbook resolver · lee firm_playbook + inyecta en skill context."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


async def get_firm_playbook(pool, firm_id: str) -> dict[str, Any]:
    """Lee firm_playbook · si no existe, retorna defaults."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select jurisdiction_default, redline_style, tone,
                   preferred_clauses, forbidden_terms, required_clauses,
                   escalation_matrix, raw_md, version, updated_at
              from firm_playbook
             where firm_id = $1::uuid
            """,
            firm_id,
        )
    if not row:
        return _default_playbook()
    d = dict(row)
    for k in ("preferred_clauses", "escalation_matrix"):
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                d[k] = {} if k == "preferred_clauses" else []
    if d.get("updated_at"):
        d["updated_at"] = d["updated_at"].isoformat()
    return d


def _default_playbook() -> dict[str, Any]:
    return {
        "jurisdiction_default": "co",
        "redline_style": "tracked",
        "tone": "formal",
        "preferred_clauses": {},
        "forbidden_terms": [],
        "required_clauses": [],
        "escalation_matrix": [],
        "raw_md": None,
        "version": 0,
        "updated_at": None,
    }


def playbook_context_block(playbook: dict[str, Any]) -> str:
    """Formatea playbook como bloque de contexto para inyectar en system prompt."""
    parts = []
    parts.append("## Contexto del despacho (playbook)")
    parts.append(f"- Jurisdicción default: **{playbook.get('jurisdiction_default', 'co')}**")
    parts.append(f"- Tono: **{playbook.get('tone', 'formal')}**")
    parts.append(f"- Estilo de redline: **{playbook.get('redline_style', 'tracked')}**")

    forbidden = playbook.get("forbidden_terms") or []
    if forbidden:
        parts.append("- **Términos prohibidos** (no usar en drafts): " + ", ".join(f"`{t}`" for t in forbidden))

    required = playbook.get("required_clauses") or []
    if required:
        parts.append("- **Cláusulas obligatorias** en contratos: " + ", ".join(f"`{c}`" for c in required))

    pref = playbook.get("preferred_clauses") or {}
    if pref and isinstance(pref, dict):
        parts.append("- **Cláusulas preferidas del despacho:**")
        for ctype, ctext in list(pref.items())[:10]:
            preview = (ctext[:120] + "…") if len(ctext) > 120 else ctext
            parts.append(f"  - `{ctype}`: {preview}")

    esc = playbook.get("escalation_matrix") or []
    if esc:
        parts.append("- **Matriz de escalación:**")
        for rule in esc[:5]:
            rango = rule.get("rango_cop", "?")
            ap = rule.get("aprobador", "?")
            parts.append(f"  - Rango {rango} → aprobador `{ap}`")

    raw_md = (playbook.get("raw_md") or "").strip()
    if raw_md:
        parts.append("\n### Reglas adicionales del despacho:\n")
        parts.append(raw_md[:5000])

    return "\n".join(parts)


async def upsert_firm_playbook(pool, firm_id: str, user_id: str,
                                 data: dict[str, Any]) -> dict[str, Any]:
    """Crea o actualiza el playbook de la firma."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            insert into firm_playbook
              (firm_id, jurisdiction_default, redline_style, tone,
               preferred_clauses, forbidden_terms, required_clauses,
               escalation_matrix, raw_md, updated_by)
            values ($1::uuid, $2, $3, $4, $5::jsonb, $6, $7, $8::jsonb, $9, $10::uuid)
            on conflict (firm_id) do update
              set jurisdiction_default = excluded.jurisdiction_default,
                  redline_style = excluded.redline_style,
                  tone = excluded.tone,
                  preferred_clauses = excluded.preferred_clauses,
                  forbidden_terms = excluded.forbidden_terms,
                  required_clauses = excluded.required_clauses,
                  escalation_matrix = excluded.escalation_matrix,
                  raw_md = excluded.raw_md,
                  updated_by = excluded.updated_by
            """,
            firm_id,
            data.get("jurisdiction_default", "co"),
            data.get("redline_style", "tracked"),
            data.get("tone", "formal"),
            json.dumps(data.get("preferred_clauses") or {}),
            data.get("forbidden_terms") or [],
            data.get("required_clauses") or [],
            json.dumps(data.get("escalation_matrix") or []),
            data.get("raw_md"),
            user_id,
        )
    return await get_firm_playbook(pool, firm_id)
