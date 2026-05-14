"""Sprint 17 · Predicción de outcome del caso usando LLM.

Lee:
  · matter (titulo, materia, cuantia, instancia, tribunal, status)
  · case_risks (top N por severity)
  · matter_documents (titulos + resumen_ia)
  · case_lessons del propio matter (si las hay)
  · similar matters via lexai_similar_matters (Sprint 17)
  · case_lessons de esos matters similares (outcome + qué funcionó)

Pide al LLM una distribución de probabilidad (won/lost/settled/abandoned)
+ confidence + resumen + recommended_strategy + top riesgos.

Inserta en case_predictions con generated_by='llm'. No hace UPSERT · cada
ejecución crea una fila nueva (audit trail · ver evolución).
"""

from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """Eres un abogado senior colombiano experto en estimar el outcome
probable de un litigio basándote en evidencia. Analizas el caso actual, los
riesgos detectados y casos similares anteriores del mismo despacho, y produces
una predicción probabilística estricta en JSON.

Formato esperado (TODOS los campos deben existir):
{
  "prob_won": 0.0-1.0,
  "prob_lost": 0.0-1.0,
  "prob_settled": 0.0-1.0,
  "prob_abandoned": 0.0-1.0,
  "confidence": 0.0-1.0,
  "primary_outcome": "won" | "lost" | "settled" | "abandoned" | "unknown",
  "summary": "3-5 líneas explicando el pronóstico y por qué",
  "recommended_strategy": "qué deberías hacer ahora · 1-3 líneas accionables",
  "top_risks": ["riesgo 1 · 1 línea", "riesgo 2", ...]
}

Reglas estrictas:
- Las 4 probabilidades deben sumar 1.0 ± 0.05.
- confidence refleja la calidad/cantidad de evidencia · si hay <2 casos
  similares con embedding o pocos datos del matter, baja confidence a 0.3-0.5.
- primary_outcome es el outcome con mayor probabilidad (o "unknown" si
  confidence < 0.2).
- top_risks: máx 5, cada uno 1 línea concreta.
- NO inventes citas. NO menciones números de sentencias que no veas en
  los datos.
- summary va al despacho: 3a persona, profesional.
"""


async def predict_outcome_for_matter(
    firm_id: str,
    matter_id: str,
    user_id: Optional[str] = None,
) -> dict:
    """Genera una nueva predicción para el matter y la persiste."""
    from utils.db import get_storage
    from utils.llm import llm_generate_json
    from utils.case_similarity import find_similar_matters, hash_inputs

    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise ValueError("Storage no disponible")

    async with storage.pool.acquire() as conn:
        matter = await conn.fetchrow(
            """
            select id, display_id, titulo, materia, status, etapa_procesal,
                   tribunal, juzgado, expediente, cuantia, cuantia_currency,
                   instance
              from matters
             where firm_id = $1::uuid and id = $2::uuid
            """,
            firm_id, matter_id,
        )
        if not matter:
            raise ValueError("Caso no encontrado")

        # Riesgos del propio caso
        risks = await _safe_fetch(conn, """
            select type, severity, title, description
              from case_risks
             where firm_id = $1::uuid and matter_id = $2::uuid
             order by severity desc
             limit 10
        """, firm_id, matter_id)

        # Documentos del caso (snippet)
        docs = await conn.fetch("""
            select titulo, kind, resumen_ia, pages
              from matter_documents
             where firm_id = $1::uuid and matter_id = $2::uuid
             order by created_at desc
             limit 8
        """, firm_id, matter_id)

        # Lessons del propio caso (si las hay)
        own_lessons = await conn.fetch("""
            select outcome, summary, strategy_used, what_worked, what_failed
              from case_lessons
             where firm_id = $1::uuid and matter_id = $2::uuid
             order by updated_at desc
             limit 3
        """, firm_id, matter_id)

        # Casos similares
        desc = (
            f"{matter['titulo']} | {matter['materia']} | "
            f"etapa: {matter['etapa_procesal'] or '?'} | "
            f"cuantia: {matter['cuantia'] or '?'} {matter['cuantia_currency'] or ''}"
        )
        similars = await find_similar_matters(conn, firm_id, matter_id, desc, limit=8)

    # ----------------------------------------------------------------
    # Construir prompt
    # ----------------------------------------------------------------
    def _trunc(s, n=240):
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
        f"- Instancia: {matter['instance'] or '—'}\n"
        f"- Tribunal: {matter['tribunal'] or '—'} · Juzgado: {matter['juzgado'] or '—'}\n"
        f"- Cuantía: {matter['cuantia'] or '—'} {matter['cuantia_currency'] or ''}".rstrip()
    )

    if risks:
        sections.append(
            "# Riesgos detectados\n"
            + "\n".join(
                f"- [{r['type']} · sev={r['severity']}] {r['title']}: {_trunc(r['description'])}"
                for r in risks
            )
        )

    if docs:
        sections.append(
            "# Documentos del expediente\n"
            + "\n".join(
                f"- [{d['kind']}] {d['titulo']}"
                + (f"\n  resumen_ia: {_trunc(d['resumen_ia'])}" if d['resumen_ia'] else "")
                for d in docs
            )
        )

    if own_lessons:
        sections.append(
            "# Lecciones ya extraídas de este mismo caso\n"
            + "\n".join(
                f"- outcome={l['outcome']} · {_trunc(l['summary'])}"
                + (f"\n  estrategia: {_trunc(l['strategy_used'])}" if l['strategy_used'] else "")
                + (f"\n  funcionó: {_trunc(l['what_worked'])}" if l['what_worked'] else "")
                + (f"\n  falló: {_trunc(l['what_failed'])}" if l['what_failed'] else "")
                for l in own_lessons
            )
        )

    if similars:
        sections.append(
            "# Casos previos similares del despacho (mayor → menor similitud)\n"
            + "\n".join(
                f"- [{s['titulo']}] outcome={s['outcome']} · sim={s['similarity']:.2f}"
                f"\n  {_trunc(s['summary'])}"
                for s in similars
            )
        )

    user_prompt = (
        "Analiza el caso, sus riesgos y los casos previos similares del despacho. "
        "Produce la predicción en JSON estricto descrito en el sistema.\n\n"
        + "\n\n".join(sections)
    )

    try:
        parsed = await llm_generate_json(
            prompt=user_prompt,
            model="gpt-4o-mini",
            system_prompt=SYSTEM_PROMPT,
            temperature=0.1,
            max_tokens=1200,
            purpose="case_prediction",
            session_id=str(user_id) if user_id else "",
        )
    except Exception as e:
        logger.exception("predict_outcome LLM falló para matter %s", matter_id)
        raise ValueError(f"LLM falló: {e}")

    p_won = _coerce_prob(parsed.get("prob_won"))
    p_lost = _coerce_prob(parsed.get("prob_lost"))
    p_settled = _coerce_prob(parsed.get("prob_settled"))
    p_abandoned = _coerce_prob(parsed.get("prob_abandoned"))
    confidence = _coerce_prob(parsed.get("confidence"))

    # Normalizar si suman muy lejos de 1.0
    total = p_won + p_lost + p_settled + p_abandoned
    if total > 0 and abs(total - 1.0) > 0.1:
        p_won, p_lost, p_settled, p_abandoned = (
            p_won / total, p_lost / total, p_settled / total, p_abandoned / total,
        )

    primary = _coerce_str(parsed.get("primary_outcome"), "unknown")
    if primary not in ("won", "lost", "settled", "abandoned", "unknown"):
        primary = "unknown"
    summary = _coerce_str(parsed.get("summary"), "(sin resumen)")
    strategy = _coerce_str(parsed.get("recommended_strategy"), None)
    top_risks = _coerce_list(parsed.get("top_risks"))[:5]

    inputs_sig = hash_inputs([
        matter["titulo"], matter["materia"], matter["status"], matter["cuantia"],
        len(risks), len(docs), len(own_lessons), len(similars),
    ])

    # Persistir
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into case_predictions
              (firm_id, matter_id, prob_won, prob_lost, prob_settled, prob_abandoned,
               confidence, primary_outcome, summary, recommended_strategy,
               risks, similar_lessons, inputs_signature, generated_by)
            values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10,
                    $11::jsonb, $12::jsonb, $13, 'llm')
            returning id, generated_at
            """,
            firm_id, matter_id, p_won, p_lost, p_settled, p_abandoned, confidence,
            primary, summary, strategy,
            json.dumps(top_risks),
            json.dumps([
                {
                    "lesson_id": s["lesson_id"],
                    "matter_id": s["matter_id"],
                    "outcome": s["outcome"],
                    "similarity": s["similarity"],
                    "titulo": s["titulo"],
                    "summary": (s["summary"] or "")[:240],
                }
                for s in similars
            ]),
            inputs_sig,
        )

    return {
        "id": str(row["id"]),
        "generated_at": row["generated_at"].isoformat() if row["generated_at"] else None,
        "prob_won": p_won,
        "prob_lost": p_lost,
        "prob_settled": p_settled,
        "prob_abandoned": p_abandoned,
        "confidence": confidence,
        "primary_outcome": primary,
        "summary": summary,
        "recommended_strategy": strategy,
        "risks": top_risks,
        "similars_used": len(similars),
        "own_lessons_used": len(own_lessons),
        "risks_input_used": len(risks),
    }


async def _safe_fetch(conn, q, *args):
    try:
        return await conn.fetch(q, *args)
    except Exception as e:
        logger.debug("safe_fetch failed: %s", e)
        return []


def _coerce_prob(v) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, x))


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


# ----------------------------------------------------------------
# Voice tool wrapper
# ----------------------------------------------------------------
async def predict_outcome_tool(args: dict, ctx: dict) -> dict:
    """Voice/agent tool · usa matter en ctx (o args.matter_id)."""
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    if not matter_id:
        return {"error": "Necesito un matter_id (estás en algún caso?)"}
    try:
        return await predict_outcome_for_matter(
            firm_id=firm_id, matter_id=matter_id, user_id=user_id,
        )
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.exception("predict_outcome_tool failed")
        return {"error": f"Fallo predicción: {e}"}
