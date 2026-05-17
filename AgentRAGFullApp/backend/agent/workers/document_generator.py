"""Multi-agent document generator · PLAN → DRAFT → CRITIC → EDIT → VALIDATE.

A lightweight state-machine that orchestrates the four primitives in
agent/llm_skills/ to produce a high-quality legal document. Avoids adding
LangGraph as a dependency · pure asyncio + clear stage functions.

State is persisted to `template_generations` (audit table from the
2026_05_24 migration). The orchestrator emits NDJSON events so the
frontend (AssistantSidebar or CanvasStreamRunner) can stream:

    {"event": "stage_started",   "data": {stage}}
    {"event": "section_started", "data": {section, idx, total}}
    {"event": "section_delta",   "data": {section, text}}
    {"event": "section_done",    "data": {section, draft_md}}
    {"event": "critic_finding",  "data": {section, severity, issue, fix}}
    {"event": "validation",      "data": {checklist_score, judge_score, ...}}
    {"event": "ready_to_send",   "data": {document_md, issues[], assumptions[]}}
    {"event": "error",           "data": {stage, error}}

The orchestrator stops gracefully on cancellation (asyncio.CancelledError).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional
from uuid import uuid4

from agent.llm_skills import (
    CritiqueFinding,
    JudgeVerdict,
    critique_section,
    judge_quality,
)
from utils.llm_tiers import Tier, get_tier_config, llm_generate_tier
from utils.playbook_resolver import get_firm_playbook, playbook_context_block

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# State
# ──────────────────────────────────────────────────────────────


@dataclass
class GenerationState:
    """Snapshot passed between stages. Mutable on purpose · keep small."""
    generation_id: str
    firm_id: str
    user_id: Optional[str]
    matter_id: Optional[str]
    channel: str                       # 'voice' | 'chat' | 'cmdk' | 'api'

    template_id: Optional[str]
    template_name: Optional[str]
    template_clauses: Optional[dict]
    template_content: Optional[str]    # raw template_md with {{slots}}
    materia: str
    doc_kind: str

    user_brief: str                    # natural language brief from user
    filled_slots: dict[str, Any] = field(default_factory=dict)
    missing_slots: list[str] = field(default_factory=list)

    plan: list[dict] = field(default_factory=list)     # [{id, title, required}, ...]
    drafts: dict[str, str] = field(default_factory=dict)
    critiques: dict[str, list[CritiqueFinding]] = field(default_factory=dict)
    edits: dict[str, str] = field(default_factory=dict)

    final_document: str = ""
    judge_verdict: Optional[JudgeVerdict] = None

    timings_ms: dict[str, int] = field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False


# ──────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────


async def run_document_generator(
    *,
    firm_id: str,
    user_id: Optional[str],
    matter_id: Optional[str],
    channel: str,
    template_id: Optional[str],
    materia: str,
    doc_kind: str,
    user_brief: str,
    pool=None,                        # asyncpg pool; if None we skip persistence
    initial_slots: Optional[dict[str, Any]] = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run the full PLAN→DRAFT→CRITIC→EDIT→VALIDATE flow as an async generator.

    Yields dicts shaped like {"event": str, "data": dict} that callers can
    forward to SSE / NDJSON streams.
    """
    state = GenerationState(
        generation_id=str(uuid4()),
        firm_id=firm_id,
        user_id=user_id,
        matter_id=matter_id,
        channel=channel,
        template_id=template_id,
        template_name=None,
        template_clauses=None,
        template_content=None,
        materia=materia,
        doc_kind=doc_kind,
        user_brief=user_brief,
        filled_slots=initial_slots or {},
    )

    # Persist initial generation row (best-effort).
    await _persist_started(pool, state)

    try:
        # 1. Load template (if any) + playbook.
        playbook_block = ""
        if pool:
            try:
                playbook = await get_firm_playbook(pool, firm_id)
                playbook_block = playbook_context_block(playbook)
            except Exception as e:
                logger.warning("playbook load failed: %s", e)

        if template_id and pool:
            await _load_template(pool, state)

        # 2. PLAN
        async for ev in _stage_plan(state):
            yield ev
            if state.cancelled:
                return

        # 3. DRAFT (per section · streaming)
        async for ev in _stage_draft(state, playbook_block):
            yield ev
            if state.cancelled:
                return

        # 4. CRITIC (per section · parallelized)
        async for ev in _stage_critic(state, playbook_block):
            yield ev
            if state.cancelled:
                return

        # 5. EDIT (apply critic suggestions)
        async for ev in _stage_edit(state, playbook_block):
            yield ev
            if state.cancelled:
                return

        # 6. VALIDATE (judge + checklist + vigencia)
        async for ev in _stage_validate(state):
            yield ev

        # 7. Final ready_to_send envelope.
        yield {
            "event": "ready_to_send",
            "data": _ready_to_send_payload(state),
        }
    except asyncio.CancelledError:
        state.cancelled = True
        state.errors.append("cancelled by client")
        raise
    except Exception as e:
        logger.exception("document_generator failed: %s", e)
        state.errors.append(str(e)[:240])
        yield {"event": "error", "data": {"stage": "orchestrator", "error": str(e)[:240]}}
    finally:
        await _persist_completed(pool, state)


# ──────────────────────────────────────────────────────────────
# Stages
# ──────────────────────────────────────────────────────────────


async def _stage_plan(state: GenerationState) -> AsyncIterator[dict[str, Any]]:
    started = time.time()
    yield {"event": "stage_started", "data": {"stage": "plan"}}

    if state.template_clauses and isinstance(state.template_clauses, dict):
        sections = state.template_clauses.get("sections") or []
        if sections:
            state.plan = [
                {
                    "id": s.get("id", f"section_{i}"),
                    "title": s.get("title", f"Sección {i+1}"),
                    "required": bool(s.get("required", True)),
                }
                for i, s in enumerate(sections)
            ]

    if not state.plan:
        # No template clauses · derive a generic legal plan from doc_kind.
        state.plan = _generic_plan_for_kind(state.doc_kind)

    state.timings_ms["plan"] = int((time.time() - started) * 1000)
    yield {
        "event": "stage_done",
        "data": {"stage": "plan", "sections": [s["title"] for s in state.plan]},
    }


async def _stage_draft(
    state: GenerationState, playbook_block: str
) -> AsyncIterator[dict[str, Any]]:
    started = time.time()
    yield {"event": "stage_started", "data": {"stage": "draft"}}

    total = len(state.plan)
    for idx, section in enumerate(state.plan):
        if state.cancelled:
            break
        yield {
            "event": "section_started",
            "data": {"section": section["id"], "title": section["title"], "idx": idx, "total": total},
        }
        try:
            draft = await _draft_one_section(state, section, playbook_block)
            state.drafts[section["id"]] = draft
            yield {
                "event": "section_done",
                "data": {"section": section["id"], "draft_md": draft},
            }
        except Exception as e:
            state.errors.append(f"draft:{section['id']}: {e}")
            yield {
                "event": "error",
                "data": {"stage": "draft", "section": section["id"], "error": str(e)[:240]},
            }

    state.timings_ms["draft"] = int((time.time() - started) * 1000)
    yield {"event": "stage_done", "data": {"stage": "draft"}}


async def _draft_one_section(
    state: GenerationState, section: dict, playbook_block: str
) -> str:
    """Generate one section's content via the Worker tier."""
    sys_prompt = (
        f"Eres un abogado senior colombiano experto en {state.materia}. "
        f"Estás redactando la sección '{section['title']}' de un(a) {state.doc_kind}. "
        "Redacta SOLO esta sección · profesional, claro, con citaciones colombianas. "
        "No incluyas el resto del documento. Usa markdown."
    )
    if playbook_block:
        sys_prompt += "\n\n" + playbook_block

    user_prompt_parts: list[str] = []
    user_prompt_parts.append(f"Brief del usuario:\n{state.user_brief}\n")
    if state.filled_slots:
        user_prompt_parts.append("Datos del caso (úsalos literalmente):\n")
        for k, v in state.filled_slots.items():
            user_prompt_parts.append(f"- {k}: {v}")
    if state.template_content and section["id"] in (state.template_content or ""):
        # Hint: if the template has a placeholder structure, include it.
        user_prompt_parts.append(
            "\nFragmento de plantilla relevante (rellena variables):\n"
            + state.template_content[:6000]
        )
    user_prompt_parts.append(f"\nRedacta la sección '{section['title']}'.")

    return await llm_generate_tier(
        Tier.WORKER,
        prompt="\n".join(user_prompt_parts),
        system_prompt=sys_prompt,
        purpose=f"drafter:{section['id']}",
        session_id=state.generation_id,
    )


async def _stage_critic(
    state: GenerationState, playbook_block: str
) -> AsyncIterator[dict[str, Any]]:
    started = time.time()
    yield {"event": "stage_started", "data": {"stage": "critic"}}

    # Parallelize critics across sections · each is independent.
    tasks: list[asyncio.Task] = []
    section_ids: list[str] = []
    for section in state.plan:
        sid = section["id"]
        draft = state.drafts.get(sid)
        if not draft:
            continue
        section_ids.append(sid)
        tasks.append(
            asyncio.create_task(
                critique_section(
                    section_name=section["title"],
                    section_text=draft,
                    materia=state.materia,
                    doc_kind=state.doc_kind,
                    playbook_md=playbook_block or None,
                    purpose=f"critic:{sid}",
                    session_id=state.generation_id,
                )
            )
        )

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for sid, r in zip(section_ids, results):
            if isinstance(r, Exception):
                state.errors.append(f"critic:{sid}: {r}")
                continue
            findings = r.data.findings  # CritiqueResult
            state.critiques[sid] = findings
            for f in findings:
                yield {
                    "event": "critic_finding",
                    "data": {
                        "section": sid,
                        "severity": f.severity,
                        "issue": f.issue,
                        "suggested_fix": f.suggested_fix,
                        "norm_reference": f.norm_reference,
                    },
                }

    state.timings_ms["critic"] = int((time.time() - started) * 1000)
    yield {"event": "stage_done", "data": {"stage": "critic"}}


async def _stage_edit(
    state: GenerationState, playbook_block: str
) -> AsyncIterator[dict[str, Any]]:
    started = time.time()
    yield {"event": "stage_started", "data": {"stage": "edit"}}

    for section in state.plan:
        if state.cancelled:
            break
        sid = section["id"]
        draft = state.drafts.get(sid, "")
        findings = state.critiques.get(sid, [])
        if not draft:
            continue
        if not findings:
            # No changes needed · pass through.
            state.edits[sid] = draft
            continue

        try:
            edited = await _edit_one_section(state, section, draft, findings, playbook_block)
            state.edits[sid] = edited
            yield {
                "event": "section_done",
                "data": {"section": sid, "edited_md": edited, "stage": "edit"},
            }
        except Exception as e:
            state.errors.append(f"edit:{sid}: {e}")
            state.edits[sid] = draft   # fallback to original draft
            yield {
                "event": "error",
                "data": {"stage": "edit", "section": sid, "error": str(e)[:240]},
            }

    # Assemble the full document from edits in plan order.
    parts = []
    for section in state.plan:
        edited = state.edits.get(section["id"]) or state.drafts.get(section["id"]) or ""
        if edited.strip():
            parts.append(edited.strip())
    state.final_document = "\n\n".join(parts)

    state.timings_ms["edit"] = int((time.time() - started) * 1000)
    yield {"event": "stage_done", "data": {"stage": "edit"}}


async def _edit_one_section(
    state: GenerationState,
    section: dict,
    draft: str,
    findings: list[CritiqueFinding],
    playbook_block: str,
) -> str:
    """Apply critic findings to a single section · WORKER tier."""
    sys_prompt = (
        f"Eres un abogado senior colombiano. Tu tarea es REVISAR el borrador "
        f"de la sección '{section['title']}' aplicando ÚNICAMENTE las "
        f"correcciones señaladas por el crítico. NO inventes contenido nuevo. "
        f"NO cambies la estructura general. Devuelve la sección corregida en "
        f"markdown."
    )
    if playbook_block:
        sys_prompt += "\n\n" + playbook_block

    findings_block = "\n".join(
        f"- [{f.severity}] {f.issue} · FIX: {f.suggested_fix}"
        for f in findings
    )
    user_prompt = (
        f"BORRADOR ACTUAL:\n{draft}\n\n"
        f"CORRECCIONES A APLICAR:\n{findings_block}\n\n"
        "Devuelve SOLO la sección corregida, sin comentarios meta."
    )

    return await llm_generate_tier(
        Tier.WORKER,
        prompt=user_prompt,
        system_prompt=sys_prompt,
        purpose=f"editor:{section['id']}",
        session_id=state.generation_id,
    )


async def _stage_validate(state: GenerationState) -> AsyncIterator[dict[str, Any]]:
    started = time.time()
    yield {"event": "stage_started", "data": {"stage": "validate"}}

    if not state.final_document.strip():
        yield {
            "event": "validation",
            "data": {"error": "empty_document"},
        }
        return

    try:
        result = await judge_quality(
            document_text=state.final_document,
            materia=state.materia,
            doc_kind=state.doc_kind,
            purpose="judge:final",
            session_id=state.generation_id,
        )
        state.judge_verdict = result.data
    except Exception as e:
        state.errors.append(f"judge: {e}")
        yield {
            "event": "validation",
            "data": {"error": str(e)[:240]},
        }
        return

    verdict = state.judge_verdict
    yield {
        "event": "validation",
        "data": {
            "judge_score": verdict.overall_score,
            "dimension_scores": verdict.dimension_scores,
            "critical_issues": verdict.critical_issues,
            "warnings": verdict.warnings,
            "strengths": verdict.strengths,
            "rationale": verdict.rationale,
        },
    }
    state.timings_ms["validate"] = int((time.time() - started) * 1000)
    yield {"event": "stage_done", "data": {"stage": "validate"}}


def _ready_to_send_payload(state: GenerationState) -> dict[str, Any]:
    """Final envelope · what the UI shows in the Ready-to-Send card."""
    issues: list[str] = []
    if state.judge_verdict:
        issues.extend(state.judge_verdict.critical_issues)
        issues.extend(state.judge_verdict.warnings)
    for sid, findings in state.critiques.items():
        for f in findings:
            if f.severity == "critical":
                issues.append(f"{sid}: {f.issue}")

    assumptions: list[str] = []
    for sl in state.missing_slots:
        assumptions.append(f"Slot sin confirmar: {sl}")

    return {
        "generation_id": state.generation_id,
        "document_md": state.final_document,
        "judge_score": state.judge_verdict.overall_score if state.judge_verdict else None,
        "dimension_scores": state.judge_verdict.dimension_scores if state.judge_verdict else None,
        "issues": issues,
        "assumptions": assumptions,
        "timings_ms": state.timings_ms,
        "errors": state.errors,
    }


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────


def _generic_plan_for_kind(doc_kind: str) -> list[dict]:
    """Fallback plan when no template_clauses are provided."""
    common = [
        {"id": "encabezado", "title": "Encabezado", "required": True},
        {"id": "partes", "title": "Partes", "required": True},
    ]
    if doc_kind in ("demanda", "demanda_civil", "demanda_laboral"):
        return common + [
            {"id": "cuantia", "title": "Cuantía", "required": True},
            {"id": "hechos", "title": "Hechos", "required": True},
            {"id": "pretensiones", "title": "Pretensiones", "required": True},
            {"id": "fundamentos", "title": "Fundamentos de derecho", "required": True},
            {"id": "pruebas", "title": "Pruebas", "required": True},
            {"id": "anexos", "title": "Anexos", "required": True},
            {"id": "notificaciones", "title": "Notificaciones", "required": True},
        ]
    if doc_kind == "tutela":
        return common + [
            {"id": "derechos", "title": "Derechos invocados", "required": True},
            {"id": "hechos", "title": "Hechos", "required": True},
            {"id": "pretensiones", "title": "Pretensiones", "required": True},
            {"id": "juramento", "title": "Juramento", "required": True},
            {"id": "pruebas", "title": "Pruebas", "required": True},
            {"id": "notificaciones", "title": "Notificaciones", "required": True},
        ]
    if doc_kind == "contestacion":
        return common + [
            {"id": "pronunciamiento_hechos", "title": "Pronunciamiento sobre hechos", "required": True},
            {"id": "excepciones", "title": "Excepciones", "required": True},
            {"id": "pruebas", "title": "Pruebas", "required": True},
            {"id": "notificaciones", "title": "Notificaciones", "required": True},
        ]
    if doc_kind == "contrato":
        return [
            {"id": "partes", "title": "Partes", "required": True},
            {"id": "objeto", "title": "Objeto", "required": True},
            {"id": "obligaciones", "title": "Obligaciones", "required": True},
            {"id": "duracion", "title": "Duración", "required": True},
            {"id": "terminacion", "title": "Terminación", "required": True},
            {"id": "ley_aplicable", "title": "Ley aplicable y solución de controversias", "required": True},
        ]
    if doc_kind == "derecho_peticion":
        return common + [
            {"id": "asunto", "title": "Asunto", "required": True},
            {"id": "peticion", "title": "Petición", "required": True},
            {"id": "fundamentos", "title": "Fundamentos", "required": True},
            {"id": "notificaciones", "title": "Notificaciones", "required": True},
        ]
    # generic memorial / recurso / otro
    return common + [
        {"id": "cuerpo", "title": "Cuerpo", "required": True},
        {"id": "peticion", "title": "Petición", "required": True},
        {"id": "notificaciones", "title": "Notificaciones", "required": True},
    ]


async def _load_template(pool, state: GenerationState) -> None:
    """Hydrate template_name, content, clauses from user_templates."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                select name, content_md, clauses_jsonb, materia::text as materia,
                       doc_type
                  from user_templates
                 where id = $1::uuid
                """,
                state.template_id,
            )
        if not row:
            return
        state.template_name = row["name"]
        state.template_content = row["content_md"]
        clauses = row["clauses_jsonb"]
        if isinstance(clauses, str):
            try:
                clauses = json.loads(clauses)
            except json.JSONDecodeError:
                clauses = None
        state.template_clauses = clauses
        # Use template's materia/doc_kind if state has 'auto' placeholder.
        if not state.materia or state.materia == "auto":
            state.materia = row["materia"] or state.materia
        if not state.doc_kind or state.doc_kind == "auto":
            state.doc_kind = row["doc_type"] or state.doc_kind
    except Exception as e:
        logger.warning("template hydrate failed: %s", e)


async def _persist_started(pool, state: GenerationState) -> None:
    if not pool:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                insert into template_generations
                  (id, firm_id, user_id, matter_id, template_id,
                   materia_at_generation, channel, status, started_at)
                values ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::uuid,
                        $6::materia_legal, $7, 'running', now())
                """,
                state.generation_id,
                state.firm_id,
                state.user_id,
                state.matter_id,
                state.template_id,
                state.materia,
                state.channel,
            )
    except Exception as e:
        logger.warning("persist_started failed (non-fatal): %s", e)


async def _persist_completed(pool, state: GenerationState) -> None:
    if not pool:
        return
    status = "succeeded"
    if state.cancelled:
        status = "cancelled"
    elif state.errors and not state.final_document:
        status = "failed"

    timings_jsonb = json.dumps(state.timings_ms)
    stages_completed = [k for k in ("plan", "draft", "critic", "edit", "validate") if k in state.timings_ms]
    checklist_score = None
    judge_score = None
    if state.judge_verdict:
        judge_score = state.judge_verdict.overall_score

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                update template_generations
                   set stages_completed = $2::text[],
                       timings_ms_jsonb = $3::jsonb,
                       judge_score = $4,
                       checklist_score = $5,
                       status = $6,
                       error_message = $7,
                       completed_at = now()
                 where id = $1::uuid
                """,
                state.generation_id,
                stages_completed,
                timings_jsonb,
                judge_score,
                checklist_score,
                status,
                "; ".join(state.errors)[:1000] if state.errors else None,
            )
    except Exception as e:
        logger.warning("persist_completed failed (non-fatal): %s", e)
