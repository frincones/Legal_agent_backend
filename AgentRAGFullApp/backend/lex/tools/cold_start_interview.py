"""Tool · cold_start_interview · onboarding guiado para nuevos firms.

Sprint M21.S2. State-machine sobre tabla cold_start_sessions (Sprint 1).

Patron Anthropic: interview de 4 partes (Part 0..3) que captura company profile,
practice areas, pain points y seed documents. Al completarse:
  - INSERT en firms_profile (company-level)
  - INSERT en practice_profile_sections (por area)
  - Opcional: INSERT en firm_seed_documents

Las tools individuales son llamadas por el Brain como ReAct steps; la UI puede
tambien orquestarlo via /v1/onboarding/* endpoints (Sprint 2.F) que delegan en este tool.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .base import ToolContext, ToolDef, ToolError

logger = logging.getLogger(__name__)


# ─── Definicion del Interview (4 partes, alineadas con Anthropic claude-for-legal) ───
COLD_START_PARTS = {
    0: {
        "label": "Identidad de la firma",
        "questions": [
            {"key": "company_name", "q": "Nombre comercial de la firma o area legal", "required": True},
            {"key": "legal_name", "q": "Razon social completa", "required": False},
            {"key": "nit", "q": "NIT (Colombia)", "required": False},
            {"key": "industry", "q": "Industria/sector principal del cliente (e.g., construccion, servicios, retail, legal)", "required": True},
            {"key": "size_employees", "q": "Tamano aproximado de la firma (numero de empleados)", "required": False},
            {"key": "practice_setting", "q": "Setting: solo / firm / in_house / government / clinic", "required": True},
            {"key": "jurisdiction", "q": "Jurisdiccion principal (default CO)", "required": False, "default": "CO"},
        ],
    },
    1: {
        "label": "Areas de practica",
        "questions": [
            {"key": "practice_areas", "q": "Lista de areas que trabaja la firma (e.g., notarial, judicial_laboral, contractual, corporate, penal)", "required": True, "type": "array"},
            {"key": "primary_area", "q": "Cual es la area dominante (>50% del trabajo)?", "required": False},
        ],
    },
    2: {
        "label": "Pain points y necesidades",
        "questions": [
            {"key": "pain_points_md", "q": "Que tareas legales repetitivas le quitan mas tiempo? (markdown libre)", "required": True},
            {"key": "tools_currently_used", "q": "Que software/templates usa hoy? (Word, Excel, sistemas judiciales, etc.)", "required": False},
            {"key": "compliance_concerns", "q": "Preocupaciones de compliance/proteccion de datos relevantes?", "required": False},
        ],
    },
    3: {
        "label": "Seed documents (opcional)",
        "questions": [
            {"key": "wants_seed_upload", "q": "Desea subir documentos modelo (poderes, contratos, demandas) para que LexAI los aprenda?", "required": False, "type": "boolean"},
            {"key": "seed_count_estimate", "q": "Cuantos documentos modelo estima subir? (0-100)", "required": False, "type": "integer"},
        ],
    },
}


class ColdStartInterviewTool(ToolDef):
    name = "cold_start_interview"
    description = (
        "★ USAR cuando el usuario es nuevo (no completo onboarding) o pide "
        "explicitamente 'iniciar configuracion'. Conduce interview de 4 partes "
        "(identidad, areas, pain points, seed docs). Cada llamada avanza una "
        "Parte: action='start' inicia, 'answer' graba respuestas de la parte "
        "actual y avanza, 'status' devuelve estado actual, 'finish' completa "
        "y persiste firms_profile + practice_profile_sections. La UI puede usar "
        "/v1/onboarding/* endpoints que delegan en este tool."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["start", "answer", "status", "finish", "abandon"],
                "description": "start: crea session; answer: graba respuestas + avanza; status: muestra estado actual; finish: persiste; abandon: marca como abandonada",
            },
            "session_id": {
                "type": "string",
                "description": "UUID de la session (devuelto por start). Requerido para answer/status/finish/abandon.",
            },
            "answers": {
                "type": "object",
                "description": "Respuestas de la parte actual (objeto JSON con keys del schema). Requerido para 'answer'.",
            },
        },
        "required": ["action"],
    }
    timeout_seconds = 10.0

    def __init__(self, pool=None, **_: Any):
        self.pool = pool

    async def run(
        self,
        ctx: ToolContext,
        action: str,
        session_id: Optional[str] = None,
        answers: Optional[dict] = None,
    ) -> dict:
        pool = self.pool or ctx.pool
        if pool is None:
            raise ToolError("Pool Supabase no disponible para cold_start_interview")
        if ctx.firm_id is None:
            raise ToolError("firm_id requerido en context")

        if action == "start":
            return await self._start(pool, ctx)
        if action == "status":
            if not session_id:
                raise ToolError("session_id requerido para status")
            return await self._status(pool, ctx, session_id)
        if action == "answer":
            if not session_id:
                raise ToolError("session_id requerido para answer")
            if not answers:
                raise ToolError("answers requerido para answer")
            return await self._answer(pool, ctx, session_id, answers)
        if action == "finish":
            if not session_id:
                raise ToolError("session_id requerido para finish")
            return await self._finish(pool, ctx, session_id)
        if action == "abandon":
            if not session_id:
                raise ToolError("session_id requerido para abandon")
            return await self._abandon(pool, ctx, session_id)
        raise ToolError(f"action desconocida: {action!r}")

    # ─── Acciones ─────────────────────────────────────────────

    async def _start(self, pool, ctx) -> dict:
        """Crea nueva session o reusa una in_progress existente."""
        async with pool.acquire() as conn:
            # Reuse session in_progress si existe
            existing = await conn.fetchrow(
                """
                select session_id, current_part, answers, status
                  from cold_start_sessions
                 where firm_id = $1 and status = 'in_progress'
                 order by created_at desc
                 limit 1
                """,
                str(ctx.firm_id),
            )
            if existing:
                logger.info("cold_start: reusing session_id=%s part=%s",
                            existing["session_id"], existing["current_part"])
                return self._render_part(
                    str(existing["session_id"]),
                    existing["current_part"],
                    dict(existing["answers"] or {}),
                    resumed=True,
                )

            new_id = await conn.fetchval(
                """
                insert into cold_start_sessions
                    (firm_id, started_by_user_id, status, current_part, answers)
                values ($1, $2, 'in_progress', 0, '{}'::jsonb)
                returning session_id
                """,
                str(ctx.firm_id),
                str(ctx.user_id) if ctx.user_id else None,
            )
        logger.info("cold_start: session creada %s", new_id)
        return self._render_part(str(new_id), 0, {}, resumed=False)

    async def _status(self, pool, ctx, session_id: str) -> dict:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                select session_id, current_part, answers, status, started_at, completed_at
                  from cold_start_sessions
                 where session_id = $1 and firm_id = $2
                """,
                session_id, str(ctx.firm_id),
            )
        if not row:
            raise ToolError(f"session {session_id!r} no encontrada")
        return {
            "session_id": str(row["session_id"]),
            "status": row["status"],
            "current_part": row["current_part"],
            "answers_so_far": dict(row["answers"] or {}),
            "total_parts": len(COLD_START_PARTS),
            "complete_pct": int(100 * row["current_part"] / len(COLD_START_PARTS)),
        }

    async def _answer(self, pool, ctx, session_id: str, new_answers: dict) -> dict:
        """Graba respuestas de la parte actual y avanza a la siguiente."""
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                select current_part, answers, status
                  from cold_start_sessions
                 where session_id = $1 and firm_id = $2
                 for update
                """,
                session_id, str(ctx.firm_id),
            )
            if not row:
                raise ToolError(f"session {session_id!r} no encontrada")
            if row["status"] != "in_progress":
                raise ToolError(f"session ya {row['status']!r}, no acepta mas respuestas")

            current_part = row["current_part"]
            current_answers = dict(row["answers"] or {})

            # Validar required de la parte actual
            part_def = COLD_START_PARTS.get(current_part)
            if part_def is None:
                raise ToolError(f"current_part {current_part} fuera de rango")
            missing = [
                q["key"] for q in part_def["questions"]
                if q.get("required") and not new_answers.get(q["key"])
            ]
            if missing:
                raise ToolError(f"Faltan campos requeridos de Parte {current_part} ({part_def['label']}): {missing}")

            # Merge bajo namespace de la parte
            part_key = f"part_{current_part}"
            current_answers[part_key] = {**(current_answers.get(part_key) or {}), **new_answers}

            next_part = current_part + 1
            is_final_part = next_part >= len(COLD_START_PARTS)

            await conn.execute(
                """
                update cold_start_sessions
                   set current_part = $1,
                       answers = $2::jsonb,
                       updated_at = now()
                 where session_id = $3
                """,
                next_part if not is_final_part else current_part,
                json.dumps(current_answers, ensure_ascii=False),
                session_id,
            )

        if is_final_part:
            return {
                "session_id": session_id,
                "status": "ready_to_finish",
                "completed_parts": next_part,
                "message": "Todas las partes respondidas. Invocar action='finish' para persistir.",
                "next_action": "finish",
            }

        return self._render_part(session_id, next_part, current_answers, resumed=False)

    async def _finish(self, pool, ctx, session_id: str) -> dict:
        """Persiste firms_profile + practice_profile_sections + marca completed."""
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    select answers, status
                      from cold_start_sessions
                     where session_id = $1 and firm_id = $2
                     for update
                    """,
                    session_id, str(ctx.firm_id),
                )
                if not row:
                    raise ToolError(f"session {session_id!r} no encontrada")
                if row["status"] == "completed":
                    return {"session_id": session_id, "status": "already_completed", "message": "Onboarding ya fue completado"}
                if row["status"] != "in_progress":
                    raise ToolError(f"status={row['status']!r}, no se puede completar")

                answers = dict(row["answers"] or {})
                p0 = answers.get("part_0", {}) or {}
                p1 = answers.get("part_1", {}) or {}
                p2 = answers.get("part_2", {}) or {}
                p3 = answers.get("part_3", {}) or {}

                # ─── 1) UPSERT firms_profile ───────────────────────
                await conn.execute(
                    """
                    insert into firms_profile
                        (firm_id, company_name, legal_name, nit, industry,
                         practice_setting, jurisdiction, size_employees, pain_points_md, metadata)
                    values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
                    on conflict (firm_id) do update set
                        company_name = excluded.company_name,
                        legal_name = excluded.legal_name,
                        nit = excluded.nit,
                        industry = excluded.industry,
                        practice_setting = excluded.practice_setting,
                        jurisdiction = excluded.jurisdiction,
                        size_employees = excluded.size_employees,
                        pain_points_md = excluded.pain_points_md,
                        metadata = excluded.metadata,
                        updated_at = now()
                    """,
                    str(ctx.firm_id),
                    p0.get("company_name") or "(sin nombre)",
                    p0.get("legal_name"),
                    p0.get("nit"),
                    p0.get("industry") or "legal_services",
                    p0.get("practice_setting") or "firm",
                    p0.get("jurisdiction") or "CO",
                    p0.get("size_employees"),
                    p2.get("pain_points_md"),
                    json.dumps({
                        "primary_area": p1.get("primary_area"),
                        "tools_currently_used": p2.get("tools_currently_used"),
                        "compliance_concerns": p2.get("compliance_concerns"),
                        "wants_seed_upload": p3.get("wants_seed_upload"),
                        "seed_count_estimate": p3.get("seed_count_estimate"),
                    }, ensure_ascii=False),
                )

                # ─── 2) INSERT practice_profile_sections (por area) ──
                areas = p1.get("practice_areas") or []
                if isinstance(areas, str):
                    areas = [a.strip() for a in areas.split(",") if a.strip()]
                primary = p1.get("primary_area")
                inserted_areas = []
                for area in areas:
                    section_id = await conn.fetchval(
                        """
                        insert into practice_profile_sections
                            (firm_id, area, is_primary, profile_md, metadata)
                        values ($1, $2, $3, $4, $5::jsonb)
                        on conflict (firm_id, area) do update set
                            is_primary = excluded.is_primary,
                            updated_at = now()
                        returning section_id
                        """,
                        str(ctx.firm_id),
                        area,
                        bool(primary and primary == area),
                        f"# {area}\n\nArea registrada en cold-start. Pendiente de detalles especificos.",
                        json.dumps({"source": "cold_start", "session_id": session_id}, ensure_ascii=False),
                    )
                    inserted_areas.append({"area": area, "section_id": str(section_id)})

                # ─── 3) Mark session completed ────────────────────
                await conn.execute(
                    """
                    update cold_start_sessions
                       set status = 'completed',
                           current_part = $1,
                           completed_at = now(),
                           updated_at = now()
                     where session_id = $2
                    """,
                    len(COLD_START_PARTS),
                    session_id,
                )

        logger.info(
            "cold_start finish: firm=%s session=%s areas=%d",
            ctx.firm_id, session_id, len(inserted_areas),
        )
        return {
            "session_id": session_id,
            "status": "completed",
            "firms_profile_created": True,
            "practice_sections_inserted": inserted_areas,
            "message": "Onboarding completado. La firma ya tiene profile + practice areas persistidas.",
            "next_action_suggested": "Subir seed documents en /v2/onboarding/seed-docs (opcional)",
        }

    async def _abandon(self, pool, ctx, session_id: str) -> dict:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                update cold_start_sessions
                   set status = 'abandoned', updated_at = now()
                 where session_id = $1 and firm_id = $2
                """,
                session_id, str(ctx.firm_id),
            )
        return {"session_id": session_id, "status": "abandoned"}

    # ─── Render helper ─────────────────────────────────────────

    def _render_part(self, session_id: str, part: int, accumulated: dict, resumed: bool) -> dict:
        part_def = COLD_START_PARTS.get(part)
        if part_def is None:
            return {
                "session_id": session_id,
                "status": "exhausted",
                "message": f"No hay Part {part}. Invocar finish.",
                "next_action": "finish",
            }
        return {
            "session_id": session_id,
            "status": "in_progress",
            "current_part": part,
            "total_parts": len(COLD_START_PARTS),
            "part_label": part_def["label"],
            "questions": part_def["questions"],
            "answers_so_far": accumulated,
            "resumed": resumed,
            "next_action": "answer",
        }


def build_tool(pool=None, **_: Any) -> ToolDef:
    return ColdStartInterviewTool(pool=pool)
