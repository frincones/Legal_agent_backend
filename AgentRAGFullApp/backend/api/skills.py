"""Sprint E · Router /v1/skills · listar + ejecutar skills."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm
from utils.skill_loader import list_active_skills
from utils.skill_runner import run_skill, run_skill_stream

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/skills", tags=["skills"])


@router.get("")
async def list_skills(principal: Principal = Depends(get_current_firm)):
    from utils.db import get_storage
    storage = await get_storage()
    return await list_active_skills(storage.pool, principal.firm_id)


class HistoryMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str


class ExecuteSkillBody(BaseModel):
    command: str = Field(..., min_length=1)
    input: dict[str, Any] = Field(default_factory=dict)
    matter_id: Optional[UUID] = None
    document_id: Optional[UUID] = None
    history: list[HistoryMessage] = Field(default_factory=list)
    # session_id permite activar session_personality_overrides (capa D3 ADR-007).
    # El frontend puede pasar el ID de conversación o tab para que los overrides
    # de sesión (brevedad, tono efímero) funcionen correctamente.
    session_id: Optional[str] = None


@router.post("/execute")
async def execute_skill(
    body: ExecuteSkillBody,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    result = await run_skill(
        storage.pool,
        firm_id=principal.firm_id,
        user_id=principal.user_id,
        command=body.command,
        input_data=body.input,
        matter_id=str(body.matter_id) if body.matter_id else None,
        document_id=str(body.document_id) if body.document_id else None,
        history=[h.model_dump() for h in body.history],
        session_id=body.session_id,
    )
    if not result.get("ok"):
        if result.get("error") == "blocked_by_hook":
            raise HTTPException(409, detail=result)
        if result.get("error") == "skill_not_found":
            raise HTTPException(404, detail=result)
        if result.get("error") == "premium_required":
            # M19.26.B — tier gate: 402 Payment Required
            raise HTTPException(402, detail=result)
        raise HTTPException(502, detail=result)
    return result


@router.post("/execute/stream")
async def execute_skill_stream(
    body: ExecuteSkillBody,
    principal: Principal = Depends(get_current_firm),
):
    """Streaming SSE de un skill · entrega tokens en tiempo real al Canvas.

    Mismo payload que /execute pero retorna text/event-stream con eventos:
      event: meta   · skill resuelto (execution_id, name)
      event: delta  · chunks de texto a medida que OpenAI los emite
      event: warning · advertencia de un post-hook
      event: done   · metadata final (duration_ms, tokens, full_text)
      event: error  · si OpenAI o un pre-hook fallan/bloquean

    Frontend recomendado: EventSource o fetch streaming + ReadableStream.
    """
    from utils.db import get_storage
    storage = await get_storage()

    async def event_generator():
        try:
            async for evt in run_skill_stream(
                storage.pool,
                firm_id=principal.firm_id,
                user_id=principal.user_id,
                command=body.command,
                input_data=body.input,
                matter_id=str(body.matter_id) if body.matter_id else None,
                document_id=str(body.document_id) if body.document_id else None,
                history=[h.model_dump() for h in body.history],
                session_id=body.session_id,
            ):
                payload = json.dumps(evt["data"], ensure_ascii=False)
                yield f"event: {evt['event']}\ndata: {payload}\n\n"
        except Exception as e:
            logger.exception("skill stream error")
            yield f"event: error\ndata: {json.dumps({'error': 'stream_failed', 'detail': str(e)[:240]})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # nginx: disable buffering
            "Connection": "keep-alive",
        },
    )


@router.get("/executions")
async def list_executions(
    matter_id: Optional[UUID] = None,
    command: Optional[str] = None,
    limit: int = 50,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        if matter_id:
            rows = await conn.fetch(
                """
                select id, command, skill_id, status, duration_ms,
                       tokens_input, tokens_output, started_at, completed_at,
                       hooks_fired, error_message
                  from skill_executions
                 where firm_id = $1::uuid and matter_id = $2::uuid
                 order by started_at desc limit $3
                """,
                principal.firm_id, matter_id, limit,
            )
        elif command:
            rows = await conn.fetch(
                """
                select id, command, skill_id, status, duration_ms,
                       tokens_input, tokens_output, started_at, completed_at,
                       hooks_fired, error_message
                  from skill_executions
                 where firm_id = $1::uuid and command = $2
                 order by started_at desc limit $3
                """,
                principal.firm_id, command, limit,
            )
        else:
            rows = await conn.fetch(
                """
                select id, command, skill_id, status, duration_ms,
                       tokens_input, tokens_output, started_at, completed_at,
                       hooks_fired, error_message
                  from skill_executions
                 where firm_id = $1::uuid
                 order by started_at desc limit $2
                """,
                principal.firm_id, limit,
            )
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        if d.get("skill_id"):
            d["skill_id"] = str(d["skill_id"])
        if d.get("started_at"):
            d["started_at"] = d["started_at"].isoformat()
        if d.get("completed_at"):
            d["completed_at"] = d["completed_at"].isoformat()
        out.append(d)
    return out


# ============================================================================
# M19.26.C · Marketplace endpoints (público y por firma)
# ============================================================================


@router.get("/marketplace")
async def list_marketplace(
    category: Optional[str] = None,
    tier: Optional[str] = None,
    include_premium: bool = True,
    principal: Principal = Depends(get_current_firm),
):
    """Lista skills builtin del marketplace público.

    Si la firma no califica para premium (firms.plan NOT IN estudio_pro/enterprise),
    los premium se devuelven con flag ``locked=true`` (visibles pero no ejecutables).

    Query params:
      category: filtrar por category (drafting | review | analysis | other)
      tier:     filtrar por tier (public | premium)
      include_premium: si false, oculta los premium completamente
    """
    from utils.db import get_storage
    storage = await get_storage()
    where = ["firm_id is null", "status = 'published'"]
    params: list[Any] = []
    if category:
        where.append(f"category = ${len(params) + 1}")
        params.append(category)
    if tier:
        where.append(f"tier = ${len(params) + 1}")
        params.append(tier)
    if not include_premium:
        where.append("tier = 'public'")

    where_clause = " and ".join(where)
    async with storage.pool.acquire() as conn:
        # Plan de la firma para decidir locked
        plan_row = await conn.fetchrow(
            "select plan from firms where id = $1::uuid", principal.firm_id,
        )
        firm_plan = (plan_row["plan"] if plan_row else None) or "trial"

        rows = await conn.fetch(
            f"""
            select id, command, name, description, category, tier,
                   jurisdiction, frontmatter, user_invocable, version, status,
                   created_at, updated_at
              from firm_skills
             where {where_clause}
             order by category nulls last, command
            """,
            *params,
        )

    premium_plans = {"estudio_pro", "enterprise"}
    out: list[dict[str, Any]] = []
    for r in rows:
        fm = r["frontmatter"]
        if isinstance(fm, str):
            try:
                fm = json.loads(fm)
            except Exception:
                fm = {}
        d_tier = (r["tier"] or "public").strip().lower()
        locked = (d_tier == "premium" and firm_plan not in premium_plans)
        out.append({
            "id": str(r["id"]),
            "command": r["command"],
            "name": r["name"],
            "description": r["description"],
            "category": r["category"],
            "tier": d_tier,
            "locked": locked,
            "jurisdiction": r["jurisdiction"],
            "frontmatter": fm or {},
            "user_invocable": r["user_invocable"],
            "version": r["version"],
            "status": r["status"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        })

    return {
        "firm_plan": firm_plan,
        "premium_unlocked": firm_plan in premium_plans,
        "count": len(out),
        "items": out,
    }


@router.get("/marketplace/{skill_id}")
async def get_marketplace_detail(
    skill_id: UUID,
    principal: Principal = Depends(get_current_firm),
):
    """Detalle de un skill del marketplace (incluye system_prompt + references_md
    SOLO si la firma tiene acceso por tier)."""
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        plan_row = await conn.fetchrow(
            "select plan from firms where id = $1::uuid", principal.firm_id,
        )
        firm_plan = (plan_row["plan"] if plan_row else None) or "trial"

        row = await conn.fetchrow(
            """
            select id, command, name, description, category, tier,
                   jurisdiction, frontmatter, system_prompt, references_md,
                   output_schema, user_invocable, version, status,
                   created_at, updated_at
              from firm_skills
             where id = $1::uuid
               and (firm_id is null or firm_id = $2::uuid)
            """,
            skill_id, principal.firm_id,
        )

    if not row:
        raise HTTPException(404, detail="skill_not_found")

    d_tier = (row["tier"] or "public").strip().lower()
    locked = d_tier == "premium" and firm_plan not in {"estudio_pro", "enterprise"}
    fm = row["frontmatter"]
    if isinstance(fm, str):
        try:
            fm = json.loads(fm)
        except Exception:
            fm = {}

    payload = {
        "id": str(row["id"]),
        "command": row["command"],
        "name": row["name"],
        "description": row["description"],
        "category": row["category"],
        "tier": d_tier,
        "locked": locked,
        "jurisdiction": row["jurisdiction"],
        "frontmatter": fm or {},
        "version": row["version"],
        "status": row["status"],
    }
    if locked:
        # No exponer el prompt completo si no tiene acceso
        payload["system_prompt"] = None
        payload["references_md"] = None
        payload["upgrade_required_plan"] = "estudio_pro"
    else:
        payload["system_prompt"] = row["system_prompt"]
        payload["references_md"] = row["references_md"]
        payload["output_schema"] = row["output_schema"]
    return payload


# ----------------------------------------------------------------------------
# M19.26.C · Learn-from-docs (async)
# ----------------------------------------------------------------------------


class LearnFromDocsBody(BaseModel):
    target_skill_command: Optional[str] = Field(
        default=None,
        pattern=r"^/[a-z0-9/-]+$",
        description="Comando del nuevo skill (ej. /redactar/contrato-firma-acme)",
    )
    hint_doc_type: Optional[str] = Field(
        default=None,
        description="Pista del usuario sobre el tipo de doc",
    )
    source_documents: list[dict[str, Any]] = Field(
        ...,
        min_length=1,
        description="Lista de {bucket, path, filename, mime, sha256}",
    )


@router.post("/learn-from-docs")
async def submit_learn_from_docs(
    body: LearnFromDocsBody,
    principal: Principal = Depends(get_current_firm),
):
    """Encola un job ``skill_learning_jobs`` que infiere un SKILL.md a partir
    de N .docx subidos previamente. Procesado por worker async (Sprint M19.30+).

    Status del job se consulta con GET /v1/skills/learn-jobs/{id}.
    """
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into skill_learning_jobs (
                firm_id, user_id, source_documents, target_skill_command,
                hint_doc_type, status
            ) values ($1::uuid, $2::uuid, $3::jsonb, $4, $5, 'queued')
            returning id, status, created_at
            """,
            principal.firm_id,
            principal.user_id,
            json.dumps(body.source_documents, ensure_ascii=False, default=str),
            body.target_skill_command,
            body.hint_doc_type,
        )
    return {
        "id": str(row["id"]),
        "status": row["status"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "message": "Job encolado. Será procesado por el worker en background.",
    }


@router.get("/learn-jobs/{job_id}")
async def get_learn_job(
    job_id: UUID,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, status, error_message, target_skill_command, hint_doc_type,
                   inferred_frontmatter, inferred_system_prompt, inferred_references_md,
                   inferred_confidence, duration_ms, tokens_input, tokens_output,
                   materialized_skill_id, created_at, completed_at, approved_at
              from skill_learning_jobs
             where id = $1::uuid and firm_id = $2::uuid
            """,
            job_id, principal.firm_id,
        )
    if not row:
        raise HTTPException(404, detail="job_not_found")
    fm = row["inferred_frontmatter"]
    if isinstance(fm, str):
        try:
            fm = json.loads(fm)
        except Exception:
            fm = {}
    return {
        "id": str(row["id"]),
        "status": row["status"],
        "error_message": row["error_message"],
        "target_skill_command": row["target_skill_command"],
        "hint_doc_type": row["hint_doc_type"],
        "inferred_frontmatter": fm,
        "inferred_system_prompt": row["inferred_system_prompt"],
        "inferred_references_md": row["inferred_references_md"],
        "inferred_confidence": float(row["inferred_confidence"]) if row["inferred_confidence"] is not None else None,
        "duration_ms": row["duration_ms"],
        "tokens_input": row["tokens_input"],
        "tokens_output": row["tokens_output"],
        "materialized_skill_id": str(row["materialized_skill_id"]) if row["materialized_skill_id"] else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
        "approved_at": row["approved_at"].isoformat() if row["approved_at"] else None,
    }


class ApproveJobBody(BaseModel):
    edits: Optional[dict[str, Any]] = Field(
        default=None,
        description="Overrides al SKILL.md inferido antes de materializar",
    )


@router.post("/learn-jobs/{job_id}/approve")
async def approve_learn_job(
    job_id: UUID,
    body: ApproveJobBody,
    principal: Principal = Depends(get_current_firm),
):
    """Aprueba un job 'succeeded' y materializa el SKILL inferido como
    firm_skills custom de la firma."""
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        async with conn.transaction():
            job = await conn.fetchrow(
                """
                select * from skill_learning_jobs
                 where id = $1::uuid and firm_id = $2::uuid
                 for update
                """,
                job_id, principal.firm_id,
            )
            if not job:
                raise HTTPException(404, detail="job_not_found")
            if job["status"] != "succeeded":
                raise HTTPException(
                    409,
                    detail={"error": "job_not_succeeded", "current_status": job["status"]},
                )

            fm = job["inferred_frontmatter"] or {}
            if isinstance(fm, str):
                try:
                    fm = json.loads(fm)
                except Exception:
                    fm = {}
            sp = job["inferred_system_prompt"] or ""
            refs = job["inferred_references_md"] or ""
            edits = body.edits or {}
            if "frontmatter" in edits:
                fm = {**fm, **(edits["frontmatter"] or {})}
            if "system_prompt" in edits and edits["system_prompt"]:
                sp = edits["system_prompt"]
            if "references_md" in edits and edits["references_md"]:
                refs = edits["references_md"]

            cmd = (edits.get("command") if edits else None) or job["target_skill_command"]
            if not cmd or not cmd.startswith("/"):
                raise HTTPException(422, detail="invalid_command")

            new_row = await conn.fetchrow(
                """
                insert into firm_skills (
                    firm_id, command, name, description, category, frontmatter,
                    system_prompt, references_md, output_schema, jurisdiction,
                    user_invocable, tier, status, version, created_by
                ) values (
                    $1::uuid, $2, $3, $4, $5, $6::jsonb,
                    $7, $8, null, 'CO',
                    true, 'public', 'published', 1, $9::uuid
                )
                returning id, command, version
                """,
                principal.firm_id,
                cmd,
                (edits.get("name") if edits else None) or fm.get("name") or cmd.rsplit("/", 1)[-1],
                fm.get("description"),
                fm.get("category") or "drafting",
                json.dumps(fm, ensure_ascii=False, default=str),
                sp,
                refs,
                principal.user_id,
            )

            await conn.execute(
                """
                update skill_learning_jobs
                   set status = 'approved',
                       materialized_skill_id = $1::uuid,
                       approved_by = $2::uuid,
                       approved_at = now()
                 where id = $3::uuid
                """,
                new_row["id"], principal.user_id, job_id,
            )

    return {
        "ok": True,
        "skill_id": str(new_row["id"]),
        "command": new_row["command"],
        "version": new_row["version"],
    }


@router.post("/learn-jobs/{job_id}/reject")
async def reject_learn_job(
    job_id: UUID,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update skill_learning_jobs
               set status = 'rejected', completed_at = now()
             where id = $1::uuid and firm_id = $2::uuid
               and status in ('queued','running','succeeded')
             returning id, status
            """,
            job_id, principal.firm_id,
        )
    if not row:
        raise HTTPException(404, detail="job_not_found_or_immutable")
    return {"ok": True, "id": str(row["id"]), "status": row["status"]}
