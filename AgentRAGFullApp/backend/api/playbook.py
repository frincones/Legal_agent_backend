"""Sprint E · Router /v1/firm/playbook · CRUD del playbook."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm
from utils.playbook_resolver import get_firm_playbook, upsert_firm_playbook

router = APIRouter(prefix="/v1/firm", tags=["playbook"])


class PlaybookUpdate(BaseModel):
    jurisdiction_default: Optional[str] = "co"
    redline_style: Optional[str] = "tracked"
    tone: Optional[str] = "formal"
    preferred_clauses: dict[str, str] = Field(default_factory=dict)
    forbidden_terms: list[str] = Field(default_factory=list)
    required_clauses: list[str] = Field(default_factory=list)
    escalation_matrix: list[dict[str, Any]] = Field(default_factory=list)
    raw_md: Optional[str] = None


@router.get("/playbook")
async def get_playbook(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    return await get_firm_playbook(storage.pool, principal.firm_id)


@router.put("/playbook")
async def update_playbook(
    body: PlaybookUpdate,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    return await upsert_firm_playbook(
        storage.pool, principal.firm_id, principal.user_id,
        body.model_dump(),
    )


# ---- M20.11 · Cold-start interview ----

class ColdStartPayload(BaseModel):
    practice_areas: list[str] = Field(default_factory=list, max_length=15)
    sample_doc_text: Optional[str] = Field(
        None, max_length=20000,
        description="Texto opcional de un MSA/contrato/poder modelo de la firma",
    )
    tone: str = Field("formal", max_length=20)
    escalation_threshold_cop: Optional[int] = None
    forbidden_terms_input: list[str] = Field(default_factory=list)


@router.post("/playbook/cold-start")
async def cold_start_playbook(
    body: ColdStartPayload,
    principal: Principal = Depends(get_current_firm),
):
    """Sprint M20.11 · genera playbook inicial vía LLM a partir de inputs del usuario.

    Útil al onboarding o cuando un despacho quiere arrancar de cero rápidamente.
    Persiste automáticamente en firm_playbook.
    """
    import json
    import logging
    from fastapi import HTTPException

    from utils.db import get_storage
    from utils.llm import get_openai_client

    logger = logging.getLogger(__name__)

    client = get_openai_client()
    if client is None:
        raise HTTPException(status_code=503, detail="openai_client_unavailable")

    sample_section = ""
    if body.sample_doc_text:
        sample_section = (
            "\n\nDocumento muestra de la firma (para inferir estilo):\n---\n"
            f"{body.sample_doc_text[:8000]}\n---"
        )

    user_msg = f"""Genera un playbook inicial (estilo CLAUDE.md) para un despacho de
abogados en Colombia.

Áreas de práctica: {', '.join(body.practice_areas) or 'general'}
Tono preferido: {body.tone}
Umbral de escalación (COP): {body.escalation_threshold_cop or 'no especificado'}
Términos prohibidos sugeridos: {', '.join(body.forbidden_terms_input) or 'ninguno aún'}{sample_section}

Devuelve JSON ESTRICTO con esta estructura exacta:
{{
  "jurisdiction_default": "co",
  "redline_style": "tracked",
  "tone": "{body.tone}",
  "preferred_clauses": {{"<doc_type>": "<cláusula preferida o nota>"}},
  "forbidden_terms": ["..."],
  "required_clauses": ["..."],
  "escalation_matrix": [{{"trigger": "monto > X", "approver": "socio"}}],
  "raw_md": "# Practice profile\\n\\n... (markdown explicando el playbook)"
}}
"""
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system",
                 "content": ("Eres un consultor de práctica legal colombiana. "
                              "Generas playbooks JSON estrictos sin texto fuera del JSON.")},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
            max_tokens=2500,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        parsed = json.loads(raw)
    except Exception as e:
        logger.warning("cold_start LLM failed: %s", e)
        raise HTTPException(status_code=500, detail=f"llm_failed: {str(e)[:200]}")

    # Persistir vía upsert
    storage = await get_storage()
    persisted = await upsert_firm_playbook(
        storage.pool, principal.firm_id, principal.user_id,
        {
            "jurisdiction_default": parsed.get("jurisdiction_default", "co"),
            "redline_style": parsed.get("redline_style", "tracked"),
            "tone": parsed.get("tone", body.tone),
            "preferred_clauses": parsed.get("preferred_clauses", {}),
            "forbidden_terms": parsed.get("forbidden_terms", []),
            "required_clauses": parsed.get("required_clauses", []),
            "escalation_matrix": parsed.get("escalation_matrix", []),
            "raw_md": parsed.get("raw_md", ""),
        },
    )
    return {
        "ok": True,
        "generated": parsed,
        "persisted": persisted,
    }


# ---- M20.12 · Playbook history (versiones archivadas) ----

@router.get("/playbook/history")
async def get_playbook_history(
    limit: int = 20,
    principal: Principal = Depends(get_current_firm),
):
    """Sprint M20.12 · lista versiones archivadas del playbook (max 20 default).

    El trigger `firm_playbook_archive_trigger` (M20.12 SQL) archiva
    automáticamente cada versión anterior en cada UPDATE material.
    """
    from utils.db import get_storage
    storage = await get_storage()
    limit = max(1, min(50, int(limit)))
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, version, jurisdiction_default, redline_style, tone,
                   forbidden_terms, required_clauses, raw_md,
                   updated_by, archived_at
            from firm_playbook_history
            where firm_id = $1::uuid
            order by version desc
            limit $2
            """,
            principal.firm_id, limit,
        )
    return {
        "history": [
            {
                "id": str(r["id"]),
                "version": r["version"],
                "jurisdiction_default": r["jurisdiction_default"],
                "redline_style": r["redline_style"],
                "tone": r["tone"],
                "forbidden_terms": list(r["forbidden_terms"] or []),
                "required_clauses": list(r["required_clauses"] or []),
                "raw_md_preview": (r["raw_md"] or "")[:500],
                "raw_md_length": len(r["raw_md"] or ""),
                "updated_by": str(r["updated_by"]) if r["updated_by"] else None,
                "archived_at": str(r["archived_at"]),
            }
            for r in rows
        ],
        "count": len(rows),
    }


@router.get("/playbook/history/{version}")
async def get_playbook_version(
    version: int,
    principal: Principal = Depends(get_current_firm),
):
    """Sprint M20.12 · recupera una versión específica con raw_md completo."""
    from fastapi import HTTPException
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select version, jurisdiction_default, redline_style, tone,
                   preferred_clauses, forbidden_terms, required_clauses,
                   escalation_matrix, raw_md, archived_at
            from firm_playbook_history
            where firm_id = $1::uuid and version = $2
            limit 1
            """,
            principal.firm_id, version,
        )
    if not row:
        raise HTTPException(status_code=404, detail="version_not_found")
    return {
        "version": row["version"],
        "jurisdiction_default": row["jurisdiction_default"],
        "redline_style": row["redline_style"],
        "tone": row["tone"],
        "preferred_clauses": row["preferred_clauses"] or {},
        "forbidden_terms": list(row["forbidden_terms"] or []),
        "required_clauses": list(row["required_clauses"] or []),
        "escalation_matrix": row["escalation_matrix"] or [],
        "raw_md": row["raw_md"] or "",
        "archived_at": str(row["archived_at"]),
    }


@router.post("/playbook/restore/{version}")
async def restore_playbook_version(
    version: int,
    principal: Principal = Depends(get_current_firm),
):
    """Sprint M20.12 · restaura una versión archivada como la actual.

    Hace upsert con los datos viejos → trigger archiva la versión actual
    como histórico antes de sobreescribir.
    """
    from fastapi import HTTPException
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select jurisdiction_default, redline_style, tone,
                   preferred_clauses, forbidden_terms, required_clauses,
                   escalation_matrix, raw_md
            from firm_playbook_history
            where firm_id = $1::uuid and version = $2
            limit 1
            """,
            principal.firm_id, version,
        )
    if not row:
        raise HTTPException(status_code=404, detail="version_not_found")
    persisted = await upsert_firm_playbook(
        storage.pool, principal.firm_id, principal.user_id,
        {
            "jurisdiction_default": row["jurisdiction_default"],
            "redline_style": row["redline_style"],
            "tone": row["tone"],
            "preferred_clauses": row["preferred_clauses"] or {},
            "forbidden_terms": list(row["forbidden_terms"] or []),
            "required_clauses": list(row["required_clauses"] or []),
            "escalation_matrix": row["escalation_matrix"] or [],
            "raw_md": row["raw_md"] or "",
        },
    )
    return {"ok": True, "restored_from_version": version, "current": persisted}
