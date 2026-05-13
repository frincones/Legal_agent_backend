"""Sprint 11 · Document compare API.

  POST /v1/doc-compare              · body { document_a_id, document_b_id }
  GET  /v1/doc-compare              · lista
  GET  /v1/doc-compare/{id}         · diff_json + diff_html + semantic_summary

Algoritmo:
  1. Cargar texto de ambos (mismo fallback de doc_qa)
  2. Diff por bloques (parrafos separados por \\n\\n) usando difflib.SequenceMatcher
  3. LLM (gpt-4o-mini) genera semantic_summary narrativo
  4. Persistir
"""

from __future__ import annotations

import difflib
import json
import logging
from html import escape
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/doc-compare", tags=["doc_compare"])


class CompareRequest(BaseModel):
    document_a_id: str
    document_b_id: str


@router.post("")
async def compare(
    body: CompareRequest,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")

    text_a, title_a = await _load_doc_text(body.document_a_id, str(principal.firm_id))
    text_b, title_b = await _load_doc_text(body.document_b_id, str(principal.firm_id))
    if not text_a or not text_b:
        raise HTTPException(409, "uno de los documentos no tiene texto procesable")

    blocks_a = [b.strip() for b in text_a.split("\n\n") if b.strip()]
    blocks_b = [b.strip() for b in text_b.split("\n\n") if b.strip()]

    sm = difflib.SequenceMatcher(a=blocks_a, b=blocks_b, autojunk=False)
    diff_json: list[dict] = []
    added = removed = changed = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i1, i2):
                diff_json.append({"op": "equal", "text": blocks_a[k]})
        elif tag == "delete":
            for k in range(i1, i2):
                diff_json.append({"op": "delete", "text": blocks_a[k]})
                removed += 1
        elif tag == "insert":
            for k in range(j1, j2):
                diff_json.append({"op": "insert", "text": blocks_b[k]})
                added += 1
        elif tag == "replace":
            # Pareo: emitir como pairs
            len_a, len_b = i2 - i1, j2 - j1
            for k in range(max(len_a, len_b)):
                a_text = blocks_a[i1 + k] if k < len_a else None
                b_text = blocks_b[j1 + k] if k < len_b else None
                if a_text and b_text:
                    diff_json.append({"op": "change", "a": a_text, "b": b_text})
                    changed += 1
                elif a_text:
                    diff_json.append({"op": "delete", "text": a_text})
                    removed += 1
                elif b_text:
                    diff_json.append({"op": "insert", "text": b_text})
                    added += 1

    diff_html = _render_html(diff_json)

    # Semantic summary
    semantic_summary = ""
    try:
        from utils.llm import get_openai_client
        client = get_openai_client()
        sample_changes = [
            d for d in diff_json
            if d.get("op") in ("insert", "delete", "change")
        ][:30]
        prompt = (
            f"Compara estos dos documentos jurídicos:\n"
            f"A: {title_a}\nB: {title_b}\n\n"
            f"Cambios detectados (max 30 bloques):\n"
            + json.dumps(sample_changes, ensure_ascii=False)[:8000]
            + "\n\nProduce un resumen narrativo en 5-8 lineas indicando los cambios "
              "JURIDICAMENTE relevantes. Espanol de Colombia."
        )
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=500,
        )
        semantic_summary = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning("doc_compare semantic summary failed: %s", e)

    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into doc_comparisons
              (firm_id, document_a_id, document_b_id, summary, diff_html, diff_json,
               added_blocks, removed_blocks, changed_blocks, semantic_summary, created_by)
            values ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6::jsonb,
                    $7, $8, $9, $10, $11::uuid)
            returning id, created_at
            """,
            principal.firm_id, body.document_a_id, body.document_b_id,
            f"{title_a} ↔ {title_b}", diff_html, json.dumps(diff_json),
            added, removed, changed, semantic_summary, principal.user_id,
        )
    return {
        "id": str(row["id"]),
        "added_blocks": added,
        "removed_blocks": removed,
        "changed_blocks": changed,
        "semantic_summary": semantic_summary,
    }


def _render_html(diff_json: list[dict]) -> str:
    parts: list[str] = []
    for d in diff_json:
        op = d.get("op")
        if op == "equal":
            parts.append(f'<p class="diff-eq">{escape(d["text"])}</p>')
        elif op == "insert":
            parts.append(f'<p class="diff-ins"><span class="badge">+</span> {escape(d["text"])}</p>')
        elif op == "delete":
            parts.append(f'<p class="diff-del"><span class="badge">−</span> {escape(d["text"])}</p>')
        elif op == "change":
            parts.append(
                '<div class="diff-change">'
                f'<p class="diff-del"><span class="badge">−</span> {escape(d["a"])}</p>'
                f'<p class="diff-ins"><span class="badge">+</span> {escape(d["b"])}</p>'
                "</div>"
            )
    return "\n".join(parts)


async def _load_doc_text(document_id: str, firm_id: str) -> tuple[str, str]:
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return "", ""
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            "select titulo, resumen_ia, ingest_doc_id from matter_documents where id = $1::uuid and firm_id = $2::uuid",
            document_id, firm_id,
        )
        if not row:
            return "", ""
        text = row["resumen_ia"] or ""
        if not text and row["ingest_doc_id"]:
            chunks = await conn.fetch(
                "select content from chunks where document_id = $1::text order by chunk_index limit 80",
                str(row["ingest_doc_id"]),
            )
            text = "\n\n".join(c["content"] for c in chunks if c["content"])
    return text, row["titulo"] if row else ""


@router.get("")
async def list_comparisons(
    document_id: Optional[str] = None,
    limit: int = Query(default=30, le=200),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    where = ["firm_id = $1::uuid"]
    params: list = [principal.firm_id]
    if document_id:
        params.append(document_id)
        where.append(f"(document_a_id = ${len(params)}::uuid or document_b_id = ${len(params)}::uuid)")
    params.append(limit)
    sql = f"""
        select id, document_a_id, document_b_id, summary, added_blocks,
               removed_blocks, changed_blocks, semantic_summary, created_at
          from doc_comparisons
         where {' and '.join(where)}
         order by created_at desc
         limit ${len(params)}
    """
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]),
                "document_a_id": str(r["document_a_id"]),
                "document_b_id": str(r["document_b_id"]),
                "summary": r["summary"],
                "added_blocks": r["added_blocks"],
                "removed_blocks": r["removed_blocks"],
                "changed_blocks": r["changed_blocks"],
                "semantic_summary": r["semantic_summary"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


@router.get("/{comparison_id}")
async def get_comparison(
    comparison_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, document_a_id, document_b_id, summary, diff_html, diff_json,
                   added_blocks, removed_blocks, changed_blocks, semantic_summary, created_at
              from doc_comparisons
             where id = $1::uuid and firm_id = $2::uuid
            """,
            comparison_id, principal.firm_id,
        )
    if not row:
        raise HTTPException(404, "not found")
    return {
        "id": str(row["id"]),
        "document_a_id": str(row["document_a_id"]),
        "document_b_id": str(row["document_b_id"]),
        "summary": row["summary"],
        "diff_html": row["diff_html"],
        "diff_json": row["diff_json"],
        "added_blocks": row["added_blocks"],
        "removed_blocks": row["removed_blocks"],
        "changed_blocks": row["changed_blocks"],
        "semantic_summary": row["semantic_summary"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


# ════════════════════════════════════════════════════════════════════════
# Voice tool
# ════════════════════════════════════════════════════════════════════════


async def compare_documents_tool(args: dict, ctx: dict) -> dict:
    """Voice: 'LexAI, compara la version 1 y la version 2 del contrato'."""
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    a = args.get("document_a_id")
    b = args.get("document_b_id")
    if not (firm_id and a and b):
        return {"error": "firm_id, document_a_id y document_b_id requeridos"}
    class _P:
        firm_id = firm_id
        user_id = user_id
        role = ctx.get("role", "lawyer")
    return await compare(CompareRequest(document_a_id=a, document_b_id=b), _P())  # type: ignore
