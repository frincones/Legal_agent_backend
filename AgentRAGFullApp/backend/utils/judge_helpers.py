"""Sprint 20 · Helpers para Judge Perspective Simulator.

  · compose_judge_profile_text · arma el texto que se embedea (perfil + decisiones)
  · fetch_judge_context        · trae el contexto completo de un juez para LLM
  · hash_document_text         · hash determinístico del escrito · permite cache
  · coerce_simulation_json     · valida + normaliza la respuesta del LLM
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


VALID_RECEPTIONS = {"favorable", "mixto", "desfavorable", "incierto"}


def compose_judge_profile_text(
    full_name: str,
    perfil: Optional[str],
    especialidades: list[str],
    recent_decisions: list[dict],
) -> str:
    """Compone el texto a embeddear para el perfil del juez."""
    parts: list[str] = [full_name]
    if especialidades:
        parts.append("Especialidades: " + ", ".join(especialidades))
    if perfil:
        parts.append(perfil)
    if recent_decisions:
        parts.append("Decisiones recientes:")
        for d in recent_decisions[:8]:
            numero = d.get("numero") or "?"
            ratio = d.get("ratio_decidendi") or d.get("decision") or ""
            if ratio:
                parts.append(f"- {numero}: {str(ratio)[:300]}")
    return "\n".join(parts)


async def fetch_judge_context(conn, judge_id: str, limit_decisions: int = 8) -> dict:
    """Trae el contexto completo del juez para usar en prompts LLM."""
    judge = await conn.fetchrow(
        """
        select id, full_name, corte, sala, cargo, ciudad,
               especialidades, perfil
          from judges
         where id = $1::uuid
        """,
        judge_id,
    )
    if not judge:
        return {}
    decisions = []
    try:
        rows = await conn.fetch(
            "select * from lexai_judge_decisions($1::uuid, $2)",
            judge_id, limit_decisions,
        )
        decisions = [
            {
                "id": str(r["id"]),
                "numero": r["numero"],
                "corte": r["corte"],
                "sala": r["sala"],
                "tipo_sentencia": r["tipo_sentencia"],
                "fecha": r["fecha"].isoformat() if r["fecha"] else None,
                "temas": list(r["temas"] or []),
                "ratio_decidendi": r["ratio_decidendi"],
                "fuente_url": r["fuente_url"],
            }
            for r in rows
        ]
    except Exception as e:
        logger.debug("fetch_judge_decisions failed for %s: %s", judge_id, e)
    return {
        "id": str(judge["id"]),
        "full_name": judge["full_name"],
        "corte": judge["corte"],
        "sala": judge["sala"],
        "cargo": judge["cargo"],
        "ciudad": judge["ciudad"],
        "especialidades": list(judge["especialidades"] or []),
        "perfil": judge["perfil"],
        "recent_decisions": decisions,
    }


def hash_document_text(text: str) -> str:
    """Hash determinístico de un texto · usado para cache + dedup."""
    h = hashlib.sha256()
    h.update((text or "").strip().encode("utf-8", errors="ignore"))
    return h.hexdigest()[:32]


def truncate_excerpt(text: str, max_chars: int = 1500) -> str:
    """Snippet para auditoría · trunca preservando inicio + fin."""
    if not text:
        return ""
    t = text.strip()
    if len(t) <= max_chars:
        return t
    head = t[: max_chars // 2]
    tail = t[-(max_chars // 2):]
    return head + "\n…\n" + tail


def coerce_simulation_json(raw: dict) -> dict:
    """Valida + normaliza la respuesta del LLM. Asegura tipos y rangos."""
    if not isinstance(raw, dict):
        return _empty_simulation()

    try:
        score = float(raw.get("alignment_score", 0))
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(1.0, score))

    reception = str(raw.get("reception") or "").strip().lower()
    if reception not in VALID_RECEPTIONS:
        # Inferir reception del score si no vino válida
        reception = (
            "favorable" if score >= 0.7
            else "mixto" if score >= 0.4
            else "desfavorable" if score >= 0.2
            else "incierto"
        )

    summary = (raw.get("summary") or "").strip()
    if not summary:
        summary = "Análisis no disponible."

    strengths = _coerce_string_list(raw.get("strengths"))[:6]
    risk_factors = _coerce_string_list(raw.get("risk_factors"))[:8]
    suggested_revisions = _coerce_string_list(raw.get("suggested_revisions"))[:6]
    similar_decisions = _coerce_dict_list(raw.get("similar_decisions"))[:5]

    return {
        "alignment_score": score,
        "reception": reception,
        "summary": summary,
        "strengths": strengths,
        "risk_factors": risk_factors,
        "suggested_revisions": suggested_revisions,
        "similar_decisions": similar_decisions,
    }


def _coerce_string_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if x and str(x).strip()]
    if isinstance(v, str):
        s = v.strip()
        return [s] if s else []
    return []


def _coerce_dict_list(v) -> list[dict]:
    if v is None:
        return []
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, dict):
                out.append({
                    "numero": str(x.get("numero") or "")[:80],
                    "fecha": str(x.get("fecha") or "")[:20],
                    "outcome": str(x.get("outcome") or "")[:50],
                    "relevance": _coerce_relevance(x.get("relevance")),
                })
        return out
    return []


def _coerce_relevance(v) -> float:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, n))


def _empty_simulation() -> dict:
    return {
        "alignment_score": 0.0,
        "reception": "incierto",
        "summary": "Análisis no disponible.",
        "strengths": [],
        "risk_factors": [],
        "suggested_revisions": [],
        "similar_decisions": [],
    }
