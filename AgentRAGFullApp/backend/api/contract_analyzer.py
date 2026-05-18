"""Sprint 11 · Contract Analyzer API.

  POST /v1/contract-analyzer/documents/{doc_id}/analyze       · dispara analisis
  GET  /v1/contract-analyzer/analyses                         · lista
  GET  /v1/contract-analyzer/analyses/{id}                    · cabecera + cl_ausulas + riesgos
  GET  /v1/contract-analyzer/documents/{doc_id}/latest        · ultimo analisis del doc
  POST /v1/contract-analyzer/risks/{id}/accept|dismiss
  GET  /v1/contract-analyzer/stats
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/contract-analyzer", tags=["contract_analyzer"])


def _serialize_analysis(r) -> dict:
    return {
        "id": str(r["id"]),
        "matter_document_id": str(r["matter_document_id"]),
        "matter_id": str(r["matter_id"]) if r["matter_id"] else None,
        "contract_type": r["contract_type"],
        "parties": r["parties"],
        "resumen_ejecutivo": r["resumen_ejecutivo"],
        "fecha_inicio": r["fecha_inicio"].isoformat() if r["fecha_inicio"] else None,
        "fecha_fin": r["fecha_fin"].isoformat() if r["fecha_fin"] else None,
        "monto_total_cop": float(r["monto_total_cop"]) if r["monto_total_cop"] is not None else None,
        "moneda": r["moneda"],
        "jurisdiccion": r["jurisdiccion"],
        "ley_aplicable": r["ley_aplicable"],
        "risk_score": r["risk_score"],
        "status": r["status"],
        "llm_model": r["llm_model"],
        "prompt_tokens": r["prompt_tokens"],
        "completion_tokens": r["completion_tokens"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


def _serialize_clause(r) -> dict:
    return {
        "id": str(r["id"]),
        "category": r["category"],
        "numero": r["numero"],
        "titulo": r["titulo"],
        "texto": r["texto"],
        "importance": r["importance"],
        "position": r["position"],
    }


def _serialize_risk(r) -> dict:
    return {
        "id": str(r["id"]),
        "clause_id": str(r["clause_id"]) if r["clause_id"] else None,
        "kind": r["kind"],
        "severity": r["severity"],
        "title": r["title"],
        "description": r["description"],
        "suggested_action": r["suggested_action"],
        "suggested_text": r["suggested_text"],
        "citations": r["citations"],
        "status": r["status"],
    }


@router.post("/documents/{document_id}/analyze")
async def analyze(
    document_id: str,
    principal: Principal = Depends(get_current_firm),
):
    """Dispara analisis. Síncrono (~10-30s)."""
    from agent.tools.contract_analyzer import analyze_contract_tool
    result = await analyze_contract_tool(
        {"document_id": document_id},
        {"firm_id": str(principal.firm_id), "user_id": str(principal.user_id)},
    )
    if "error" in result:
        # Mapeo amigable
        code = result.get("error")
        if code == "texto_insuficiente":
            raise HTTPException(409, result.get("detail") or "documento sin texto")
        if code == "llm_failed":
            raise HTTPException(502, result.get("detail") or "LLM falló")
        raise HTTPException(400, result.get("detail") or code)
    return result


@router.get("/analyses")
async def list_analyses(
    matter_document_id: Optional[str] = None,
    matter_id: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    where = ["firm_id = $1::uuid"]
    params: list = [principal.firm_id]
    if matter_document_id:
        params.append(matter_document_id); where.append(f"matter_document_id = ${len(params)}::uuid")
    if matter_id:
        params.append(matter_id); where.append(f"matter_id = ${len(params)}::uuid")
    params.append(limit)
    sql = f"""
        select id, matter_document_id, matter_id, contract_type, parties,
               resumen_ejecutivo, fecha_inicio, fecha_fin, monto_total_cop, moneda,
               jurisdiccion, ley_aplicable, risk_score, status, llm_model,
               prompt_tokens, completion_tokens, created_at
          from contract_analyses
         where {' and '.join(where)}
         order by created_at desc
         limit ${len(params)}
    """
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {"count": len(rows), "items": [_serialize_analysis(r) for r in rows]}


@router.get("/documents/{document_id}/latest")
async def latest_for_document(
    document_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id from contract_analyses
             where matter_document_id = $1::uuid and firm_id = $2::uuid and status = 'completed'
             order by created_at desc limit 1
            """,
            document_id, principal.firm_id,
        )
    if not row:
        return {"latest": None}
    # Reusar get_analysis
    return await get_analysis(str(row["id"]), principal)


@router.get("/analyses/{analysis_id}")
async def get_analysis(
    analysis_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        a = await conn.fetchrow(
            """
            select id, matter_document_id, matter_id, contract_type, parties,
                   resumen_ejecutivo, fecha_inicio, fecha_fin, monto_total_cop, moneda,
                   jurisdiccion, ley_aplicable, risk_score, status, llm_model,
                   prompt_tokens, completion_tokens, created_at
              from contract_analyses
             where id = $1::uuid and firm_id = $2::uuid
            """,
            analysis_id, principal.firm_id,
        )
        if not a:
            raise HTTPException(404, "not found")
        clauses = await conn.fetch(
            """
            select id, category, numero, titulo, texto, importance, position
              from contract_clauses where analysis_id = $1::uuid
             order by position asc
            """,
            analysis_id,
        )
        risks = await conn.fetch(
            """
            select id, clause_id, kind, severity, title, description,
                   suggested_action, suggested_text, citations, status
              from contract_risks where analysis_id = $1::uuid
             order by case severity when 'critico' then 1 when 'alto' then 2 when 'medio' then 3 else 4 end
            """,
            analysis_id,
        )
    return {
        "analysis": _serialize_analysis(a),
        "clauses": [_serialize_clause(c) for c in clauses],
        "risks": [_serialize_risk(r) for r in risks],
    }


class RiskStatusRequest(BaseModel):
    note: Optional[str] = None


@router.post("/risks/{risk_id}/accept")
async def accept_risk(
    risk_id: str,
    body: Optional[RiskStatusRequest] = None,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update contract_risks set status = 'accepted',
                                      resolved_at = now(), resolved_by = $3::uuid
             where id = $1::uuid and firm_id = $2::uuid
             returning id, status
            """,
            risk_id, principal.firm_id, principal.user_id,
        )
    if not row:
        raise HTTPException(404, "not found")
    return {"id": str(row["id"]), "status": row["status"]}


@router.post("/risks/{risk_id}/dismiss")
async def dismiss_risk(
    risk_id: str,
    body: Optional[RiskStatusRequest] = None,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update contract_risks set status = 'dismissed',
                                      resolved_at = now(), resolved_by = $3::uuid
             where id = $1::uuid and firm_id = $2::uuid
             returning id, status
            """,
            risk_id, principal.firm_id, principal.user_id,
        )
    if not row:
        raise HTTPException(404, "not found")
    return {"id": str(row["id"]), "status": row["status"]}


@router.get("/stats")
async def stats(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        data = await conn.fetchval(
            "select lexai_contract_stats($1::uuid)", principal.firm_id,
        )
    return data or {}


# ════════════════════════════════════════════════════════════════════════
# Voice tool
# ════════════════════════════════════════════════════════════════════════


async def analyze_contract_voice_tool(args: dict, ctx: dict) -> dict:
    """Voice: 'LexAI, analiza el contrato que acabo de subir'."""
    from agent.tools.contract_analyzer import analyze_contract_tool
    from agent.tools._ui_events import ui_data_changed
    result = await analyze_contract_tool(args, ctx)
    if isinstance(result, dict) and not result.get("error"):
        result["_ui_command"] = ui_data_changed(
            "documents", matter_id=ctx.get("matter_id"), firm_id=ctx.get("firm_id"),
            op="update", extra={
                "document_id": args.get("document_id"),
                "analysis_id": result.get("id"),
                "kind": "contract_analysis",
            },
        )
    return result
