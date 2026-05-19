"""Sprint 20 · M25 Judge Perspective Simulator.

Dado un juez (judge_id) y un escrito (texto + matter_id opcional), LLM produce:
  - alignment_score (0..1) · qué tan alineado está con la línea del juez
  - reception (favorable / mixto / desfavorable / incierto)
  - summary 3-5 líneas
  - strengths · argumentos que el juez probablemente valorará
  - risk_factors · puntos que el juez probablemente cuestionará
  - suggested_revisions · cómo mejorar el escrito
  - similar_decisions · referencias del juez relevantes

NO inventa decisiones que no estén en el contexto. Si no encuentra similar
decisions reales, devuelve [] vacío. Esto preserva la trazabilidad legal.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """Eres un abogado litigante senior colombiano experto en analizar
cómo un juez específico recibirá un escrito jurídico. Recibes:
  1) Perfil del juez (corte, sala, especialidades, línea jurisprudencial)
  2) Sus decisiones recientes con ratio decidendi
  3) El texto del escrito a analizar

Tu trabajo es predecir la recepción del escrito por este juez. Devuelve UN
SOLO objeto JSON con esta estructura estricta:

{
  "alignment_score": 0.0-1.0,
  "reception": "favorable" | "mixto" | "desfavorable" | "incierto",
  "summary": "3-5 líneas explicando la predicción",
  "strengths": [
    "argumento que el juez probablemente valorará · 1-2 líneas",
    ...
  ],
  "risk_factors": [
    "punto débil que el juez probablemente cuestionará · 1-2 líneas",
    ...
  ],
  "suggested_revisions": [
    "cómo mejorar el escrito · acción concreta",
    ...
  ],
  "similar_decisions": [
    {"numero": "T-XXX/AAAA", "fecha": "AAAA-MM-DD", "outcome": "favorable/desfavorable/parcial", "relevance": 0.0-1.0},
    ...
  ]
}

Reglas estrictas:
- alignment_score · 0=opuesto a la línea del juez, 1=perfectamente alineado.
- reception se infiere del score: ≥0.7 favorable, 0.4-0.7 mixto, 0.2-0.4 desfavorable, <0.2 incierto.
- strengths: máx 5, cada uno 1-2 líneas, concretos.
- risk_factors: máx 6, cada uno apunta a un argumento específico del juez.
- suggested_revisions: máx 5, accionables ("Cita T-388/2019" mejor que "Mejorar fundamentación").
- similar_decisions: SOLO incluye decisiones que aparezcan en el contexto proporcionado.
  NO inventes números de sentencia. Si no hay match claro, devuelve [].
- summary va al abogado: 3a persona, profesional, sin alucinaciones.
- NO menciones nombres del juez explícitamente · refiérete como "el juez" o "la magistrada".
"""


async def simulate_judge_view_for_matter(
    firm_id: str,
    matter_id: str,
    judge_id: str,
    document_text: Optional[str] = None,
    user_id: Optional[str] = None,
    use_cache: bool = True,
) -> dict:
    """Corre la simulación + persiste en judge_predictions."""
    from utils.db import get_storage
    from utils.llm import llm_generate_json
    from utils.judge_helpers import (
        fetch_judge_context, hash_document_text, truncate_excerpt, coerce_simulation_json,
    )

    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise ValueError("Storage no disponible")

    text = (document_text or "").strip()

    async with storage.pool.acquire() as conn:
        judge_ctx = await fetch_judge_context(conn, judge_id, limit_decisions=8)
        if not judge_ctx:
            raise ValueError("Juez no encontrado")

        # Si no nos dan document_text, intentar componer desde el matter
        if not text:
            text = await _compose_matter_text(conn, firm_id, matter_id)
        if not text:
            raise ValueError("No hay escrito ni contexto del matter para analizar")

        doc_hash = hash_document_text(text)
        excerpt = truncate_excerpt(text, max_chars=2400)

        # Cache hit · misma combinación matter+judge+hash en últimas 24h
        if use_cache:
            cached = await conn.fetchrow(
                """
                select id, alignment_score, reception, summary, strengths,
                       risk_factors, suggested_revisions, similar_decisions,
                       generated_at
                  from judge_predictions
                 where firm_id = $1::uuid and matter_id = $2::uuid
                   and judge_id = $3::uuid and document_hash = $4
                   and generated_at > now() - interval '24 hours'
                 order by generated_at desc limit 1
                """,
                firm_id, matter_id, judge_id, doc_hash,
            )
            if cached:
                return _serialize(cached, judge_ctx, cached=True)

    # ----------------------------------------------------------------
    # Build prompt
    # ----------------------------------------------------------------
    def _trunc(s, n=300):
        if not s:
            return ""
        s = str(s)
        return s if len(s) <= n else s[:n] + "…"

    judge_block = (
        f"# Juez\n"
        f"- Corte: {judge_ctx['corte']}\n"
        f"- Sala: {judge_ctx.get('sala') or '—'}\n"
        f"- Cargo: {judge_ctx.get('cargo') or '—'}\n"
        f"- Especialidades: {', '.join(judge_ctx.get('especialidades') or []) or '—'}\n"
        f"- Perfil:\n{judge_ctx.get('perfil') or '(sin perfil documentado)'}"
    )

    decisions = judge_ctx.get("recent_decisions") or []
    decisions_block = ""
    if decisions:
        decisions_block = (
            "# Decisiones recientes del juez (más nueva primero)\n"
            + "\n".join(
                f"- [{d.get('numero') or '?'}] {d.get('corte', '')}/{d.get('sala', '')} · "
                f"{d.get('fecha') or '?'}"
                + (f"\n  Ratio: {_trunc(d.get('ratio_decidendi'), 300)}" if d.get("ratio_decidendi") else "")
                + (f"\n  Temas: {', '.join(d.get('temas') or [])}" if d.get("temas") else "")
                for d in decisions[:8]
            )
        )
    else:
        decisions_block = (
            "# Decisiones recientes del juez\n"
            "(no hay decisiones del juez indexadas; el análisis se basa en su perfil declarado)"
        )

    document_block = f"# Escrito a analizar\n{excerpt}"

    user_prompt = (
        "Analiza cómo este juez recibirá el escrito. Produce el JSON estricto "
        "descrito en el sistema.\n\n"
        + judge_block + "\n\n"
        + decisions_block + "\n\n"
        + document_block
    )

    try:
        parsed = await llm_generate_json(
            prompt=user_prompt,
            model="gpt-4o-mini",
            system_prompt=SYSTEM_PROMPT,
            temperature=0.1,
            max_tokens=1500,
            purpose="judge_simulation",
            session_id=str(user_id) if user_id else "",
        )
    except Exception as e:
        logger.exception("judge_simulation LLM falló")
        raise ValueError(f"LLM falló: {e}")

    sim = coerce_simulation_json(parsed)

    # Persistir
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into judge_predictions
              (firm_id, matter_id, judge_id, document_excerpt, document_hash,
               alignment_score, reception, summary, strengths, risk_factors,
               suggested_revisions, similar_decisions, generated_by, model_used)
            values ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7, $8,
                    $9::jsonb, $10::jsonb, $11::jsonb, $12::jsonb,
                    'llm', $13)
            returning id, generated_at
            """,
            firm_id, matter_id, judge_id,
            excerpt[:2000], doc_hash,
            sim["alignment_score"], sim["reception"], sim["summary"],
            json.dumps(sim["strengths"]),
            json.dumps(sim["risk_factors"]),
            json.dumps(sim["suggested_revisions"]),
            json.dumps(sim["similar_decisions"]),
            "gpt-4o-mini",
        )

    return {
        "id": str(row["id"]),
        "generated_at": row["generated_at"].isoformat() if row["generated_at"] else None,
        "judge_id": str(judge_id),
        "judge_name": judge_ctx["full_name"],
        "alignment_score": sim["alignment_score"],
        "reception": sim["reception"],
        "summary": sim["summary"],
        "strengths": sim["strengths"],
        "risk_factors": sim["risk_factors"],
        "suggested_revisions": sim["suggested_revisions"],
        "similar_decisions": sim["similar_decisions"],
        "cached": False,
    }


async def _compose_matter_text(conn, firm_id: str, matter_id: str) -> str:
    """Si no nos dan document_text, intenta componer texto del matter:
       titulo + cliente + descripción del primer doc con resumen_ia."""
    matter = await conn.fetchrow(
        """
        select m.titulo, m.materia, m.etapa_procesal,
               m.cuantia, m.cuantia_currency,
               c.nombre as cliente_nombre
          from matters m
          left join clients c on c.id = m.client_id
         where m.firm_id = $1::uuid and m.id = $2::uuid
        """,
        firm_id, matter_id,
    )
    if not matter:
        return ""
    parts = [
        f"Caso: {matter['titulo']}",
        f"Materia: {matter['materia']}",
        f"Cliente: {matter['cliente_nombre'] or '—'}",
        f"Etapa: {matter['etapa_procesal'] or '—'}",
    ]
    if matter["cuantia"]:
        parts.append(f"Cuantía: {matter['cuantia']} {matter['cuantia_currency'] or ''}")
    try:
        doc = await conn.fetchrow(
            """
            select titulo, resumen_ia from matter_documents
             where firm_id = $1::uuid and matter_id = $2::uuid
               and resumen_ia is not null
             order by created_at desc limit 1
            """,
            firm_id, matter_id,
        )
        if doc and doc["resumen_ia"]:
            parts.append(f"Resumen documento principal:\n{doc['resumen_ia']}")
    except Exception:
        pass
    return "\n\n".join(parts)


def _serialize(row, judge_ctx: dict, cached: bool = False) -> dict:
    return {
        "id": str(row["id"]),
        "generated_at": row["generated_at"].isoformat() if row["generated_at"] else None,
        "judge_id": judge_ctx["id"],
        "judge_name": judge_ctx["full_name"],
        "alignment_score": float(row["alignment_score"] or 0),
        "reception": row["reception"],
        "summary": row["summary"],
        "strengths": _parse_json(row["strengths"]),
        "risk_factors": _parse_json(row["risk_factors"]),
        "suggested_revisions": _parse_json(row["suggested_revisions"]),
        "similar_decisions": _parse_json(row["similar_decisions"]),
        "cached": cached,
    }


def _parse_json(v):
    if v is None:
        return []
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return []
    return v


# ----------------------------------------------------------------
# Voice tool wrapper
# ----------------------------------------------------------------
async def simulate_judge_view_tool(args: dict, ctx: dict) -> dict:
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    judge_id = args.get("judge_id")
    document_text = args.get("document_text") or args.get("text")
    if not firm_id:
        return {"error": "firm_id requerido"}
    if not matter_id:
        return {"error": "Necesito matter_id (estás en algún caso?)"}
    if not judge_id:
        # Fallback al primer juez seed (matters no tiene judge_id explícito).
        from utils.db import get_storage
        _s = await get_storage()
        if hasattr(_s, "pool"):
            async with _s.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "select id from judges order by created_at limit 1"
                )
                if row:
                    judge_id = str(row["id"])
    if not judge_id:
        return {"error": "Necesito judge_id · usa search_judge primero"}
    try:
        return await simulate_judge_view_for_matter(
            firm_id=firm_id, matter_id=matter_id, judge_id=judge_id,
            document_text=document_text, user_id=user_id,
        )
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.exception("simulate_judge_view_tool failed")
        return {"error": f"Fallo simulación: {e}"}
