"""Sprint 15 · Extract lessons learned from a closed matter.

Lee:
  · matters (titulo, materia, status, cuantia, instancia, tribunal)
  · case_events (últimos N · qué pasó)
  · matter_documents (titulos + resumen_ia disponibles)
  · case_risks (riesgos detectados)
  · time_entries / expenses (esfuerzo invertido) — opcional

Pide al LLM un JSON estricto con:
  outcome, summary, strategy_used, what_worked, what_failed,
  key_citations[], key_arguments[], tags[]

Inserta/UPSERT en case_lessons con generated_by='llm_curated' y embed.
Idempotente por (matter_id, generated_by) — al volver a llamar con
force=True actualiza la existente.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """Eres un abogado senior colombiano que documenta lecciones aprendidas
de casos cerrados para construir la "memoria del despacho". Tu trabajo es leer
el resumen de un caso (datos del caso, eventos, documentos, riesgos detectados)
y producir una lección concisa, accionable, en JSON estricto.

Formato esperado (todos los campos deben existir, usa string vacío "" o array
vacío [] cuando no haya información):
{
  "outcome": "won" | "lost" | "settled" | "abandoned" | "unknown",
  "summary": "3-5 líneas describiendo qué pasó y por qué",
  "strategy_used": "la estrategia legal principal en 1-2 líneas",
  "what_worked": "qué decisiones / argumentos / movimientos funcionaron (vacío si lost)",
  "what_failed": "qué falló o pudo hacerse mejor (vacío si won limpio)",
  "key_citations": ["citas relevantes: ej. T-388/2019, CST Art. 64"],
  "key_arguments": ["argumentos clave usados o que faltaron"],
  "tags": ["tags cortos en minúsculas para clasificación"]
}

Reglas:
- Sé específico y útil. Una lección que un abogado júnior pueda usar el próximo caso similar.
- No inventes citas que no veas en los datos. Mejor [] vacío que falso.
- summary va dirigido al despacho, no al cliente · habla en 3a persona.
- tags: máx 6, minúsculas, sin tildes ni espacios (usa "_").
- Si no hay suficiente contexto para inferir outcome, devuelve "unknown".
"""


async def extract_lesson_from_matter(
    firm_id: str,
    matter_id: str,
    user_id: Optional[str] = None,
    force: bool = False,
) -> dict:
    """Genera (o re-genera con force=True) una lesson llm_curated para el matter."""
    from utils.db import get_storage
    from utils.llm import llm_generate_json
    from utils.embeddings import embed_text_as_pg, compose_lesson_text

    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise ValueError("Storage no disponible")

    async with storage.pool.acquire() as conn:
        matter = await conn.fetchrow(
            """
            select id, display_id, titulo, materia, status, etapa_procesal,
                   tribunal, juzgado, expediente, cuantia, cuantia_currency,
                   created_at, updated_at
              from matters
             where firm_id = $1::uuid and id = $2::uuid
            """,
            firm_id, matter_id,
        )
        if not matter:
            raise ValueError("Caso no encontrado")
        if matter["status"] not in ("cerrado", "archivado"):
            # Permitimos extraer aunque no esté cerrado, pero lo anotamos.
            logger.info(
                "extract_lesson · matter %s no está cerrado (status=%s) · se extraerá igual",
                matter_id, matter["status"],
            )

        if not force:
            existing = await conn.fetchrow(
                """
                select id from case_lessons
                 where firm_id = $1::uuid and matter_id = $2::uuid
                   and generated_by in ('llm','llm_curated')
                """,
                firm_id, matter_id,
            )
            if existing:
                return {
                    "status": "already_exists",
                    "lesson_id": str(existing["id"]),
                    "note": "Use force=true para re-extraer",
                }

        events = await conn.fetch(
            """
            select titulo, descripcion, kind, fecha
              from case_events
             where firm_id = $1::uuid and matter_id = $2::uuid
             order by fecha desc nulls last
             limit 30
            """,
            firm_id, matter_id,
        ) if await _table_exists(conn, "case_events") else []

        docs = await conn.fetch(
            """
            select titulo, kind, resumen_ia, pages
              from matter_documents
             where firm_id = $1::uuid and matter_id = $2::uuid
             order by created_at desc
             limit 25
            """,
            firm_id, matter_id,
        )

        risks = await conn.fetch(
            """
            select type, severity, title, description, mitigation
              from case_risks
             where firm_id = $1::uuid and matter_id = $2::uuid
             order by severity desc
             limit 20
            """,
            firm_id, matter_id,
        ) if await _table_exists(conn, "case_risks") else []

    # ----------------------------------------------------------------
    # Build prompt
    # ----------------------------------------------------------------
    def _trunc(s: Optional[str], n: int = 600) -> str:
        if not s:
            return ""
        s = str(s)
        return s if len(s) <= n else s[:n] + "…"

    sections: list[str] = []
    sections.append(
        f"# Caso\n"
        f"- ID: {matter['display_id']}\n"
        f"- Título: {matter['titulo']}\n"
        f"- Materia: {matter['materia']}\n"
        f"- Estado: {matter['status']}\n"
        f"- Etapa procesal: {matter['etapa_procesal'] or '—'}\n"
        f"- Tribunal: {matter['tribunal'] or '—'}\n"
        f"- Juzgado: {matter['juzgado'] or '—'}\n"
        f"- Expediente: {matter['expediente'] or '—'}\n"
        f"- Cuantía: {matter['cuantia'] or '—'} {matter['cuantia_currency'] or ''}".rstrip()
    )

    if events:
        sections.append(
            "# Eventos recientes (más nuevo primero)\n"
            + "\n".join(
                f"- [{e['kind']}] {e['titulo']}: {_trunc(e['descripcion'], 200)}"
                for e in events[:15]
            )
        )
    if docs:
        sections.append(
            "# Documentos del caso\n"
            + "\n".join(
                f"- [{d['kind']}] {d['titulo']}"
                + (f"\n  resumen_ia: {_trunc(d['resumen_ia'], 300)}" if d.get("resumen_ia") else "")
                for d in docs[:12]
            )
        )
    if risks:
        sections.append(
            "# Riesgos detectados\n"
            + "\n".join(
                f"- [{r['type']} · sev={r['severity']}] {r['title']}: {_trunc(r['description'], 200)}"
                for r in risks
            )
        )

    user_prompt = (
        "Analiza el siguiente caso cerrado/archivado y produce la lección aprendida "
        "en el JSON estricto descrito en el sistema.\n\n"
        + "\n\n".join(sections)
    )

    # ADR-007 Fase 4 · prefijación con módulos safety/output/channel si SUBAGENT=true
    effective_system_prompt = SYSTEM_PROMPT
    try:
        from utils import persona_assembler
        inherited, _, _ = await persona_assembler.get_assembled_system_prompt(
            pool=storage.pool,
            firm_id=firm_id,
            user_id=user_id,
            channel="chat",
            skill="subagent",
            session_id=None,
            legacy_prompt="",
        )
        if inherited:
            effective_system_prompt = inherited + "\n\n---\n\n" + SYSTEM_PROMPT
    except Exception as _pa_exc:
        logger.warning("extract_lessons: persona_assembler falló · usando SYSTEM_PROMPT original. %s", _pa_exc)

    try:
        parsed = await llm_generate_json(
            prompt=user_prompt,
            model="gpt-4o-mini",
            system_prompt=effective_system_prompt,
            temperature=0.2,
            max_tokens=1500,
            purpose="case_lesson_extract",
            session_id=str(user_id) if user_id else "",
        )
    except Exception as e:
        logger.exception("LLM falló extrayendo lección para matter %s", matter_id)
        raise ValueError(f"LLM falló: {e}")

    outcome = _coerce_str(parsed.get("outcome"), "unknown")
    if outcome not in ("won", "lost", "settled", "abandoned", "unknown"):
        outcome = "unknown"
    summary = _coerce_str(parsed.get("summary"), "(sin resumen)")
    strategy_used = _coerce_str(parsed.get("strategy_used"), None)
    what_worked = _coerce_str(parsed.get("what_worked"), None)
    what_failed = _coerce_str(parsed.get("what_failed"), None)
    key_citations = _coerce_list(parsed.get("key_citations"))
    key_arguments = _coerce_list(parsed.get("key_arguments"))
    tags = [
        t.strip().lower().replace(" ", "_")[:40]
        for t in _coerce_list(parsed.get("tags"))
        if isinstance(t, str) and t.strip()
    ][:6]

    title_hint = f"Lección · {matter['titulo']} · {outcome}"
    lesson_text = " · ".join(s for s in [summary, strategy_used, what_worked, what_failed] if s)
    embedding_pg = await embed_text_as_pg(
        compose_lesson_text(title_hint, lesson_text, tags),
        purpose="case_lesson_extract_embed",
        session_id=str(user_id) if user_id else "",
    )

    # ----------------------------------------------------------------
    # Upsert · una lección llm_curated por matter
    # ----------------------------------------------------------------
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into case_lessons
              (firm_id, matter_id, outcome, summary, strategy_used,
               what_worked, what_failed, key_citations, key_arguments,
               tags, generated_by, embedding, embedding_at)
            values ($1::uuid, $2::uuid, $3, $4, $5,
                    $6, $7, $8::jsonb, $9::jsonb,
                    $10, 'llm_curated',
                    case when $11::text is not null then $11::vector else null end,
                    case when $11::text is not null then now() else null end)
            on conflict (matter_id, generated_by) do update
              set outcome = excluded.outcome,
                  summary = excluded.summary,
                  strategy_used = excluded.strategy_used,
                  what_worked = excluded.what_worked,
                  what_failed = excluded.what_failed,
                  key_citations = excluded.key_citations,
                  key_arguments = excluded.key_arguments,
                  tags = excluded.tags,
                  embedding = excluded.embedding,
                  embedding_at = excluded.embedding_at,
                  updated_at = now()
            returning id, generated_by, created_at, updated_at
            """,
            firm_id, matter_id, outcome, summary,
            strategy_used, what_worked, what_failed,
            json.dumps(key_citations), json.dumps(key_arguments),
            tags, embedding_pg,
        )

    return {
        "status": "ok",
        "lesson_id": str(row["id"]),
        "generated_by": row["generated_by"],
        "outcome": outcome,
        "summary": summary,
        "strategy_used": strategy_used,
        "what_worked": what_worked,
        "what_failed": what_failed,
        "key_citations": key_citations,
        "key_arguments": key_arguments,
        "tags": tags,
        "embedded": embedding_pg is not None,
    }


# ----------------------------------------------------------------
# Voice tool wrapper · invoca extract_lesson_from_matter desde la voz
# ----------------------------------------------------------------
async def extract_lesson_tool(args: dict, ctx: dict) -> dict:
    """Voice/agent tool: extrae lección del matter en contexto (o args.matter_id)."""
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    if not matter_id:
        return {"error": "Necesito un matter_id (estás en algún caso?)"}
    try:
        result = await extract_lesson_from_matter(
            firm_id=firm_id,
            matter_id=matter_id,
            user_id=user_id,
            force=bool(args.get("force", False)),
        )
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.exception("extract_lesson_tool failed")
        return {"error": f"Fallo extracción: {e}"}
    if isinstance(result, dict) and not result.get("error"):
        from agent.tools._ui_events import ui_data_changed
        result["_ui_command"] = ui_data_changed(
            "lessons", matter_id=matter_id, firm_id=firm_id, op="create",
            extra={"lesson_id": result.get("lesson_id")},
        )
    return result


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------
async def _table_exists(conn, name: str) -> bool:
    return bool(
        await conn.fetchval(
            "select exists(select 1 from information_schema.tables where table_name = $1)",
            name,
        )
    )


def _coerce_str(v, default):
    if v is None:
        return default
    s = str(v).strip()
    return s if s else default


def _coerce_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x is not None]
    if isinstance(v, str):
        return [v] if v.strip() else []
    return []
