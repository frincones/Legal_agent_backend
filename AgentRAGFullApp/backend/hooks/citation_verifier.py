"""Hook citation_verifier · Sprint L3.

Se invoca después de la ejecución de un skill (`post_skill`) y verifica
las citas legales generadas por el LLM contra fuentes oficiales en vivo:
  - Sentencias (T, C, SU, SC, SL, SP) → Corte Constitucional
  - Leyes/Decretos → Secretaría del Senado
  - Códigos (CST, CGP, C.C., C.CO.) → Senado/Función Pública

Decisión:
  - approve: todas las citas verificadas
  - warn:    1+ citas sospechosas (alucinación posible)

Decision_mode='warn' en BD, así que aunque el hook diga 'block', el runner
lo degrada a 'warn'. El frontend muestra las sospechosas como banner.

Sprint L · usa utils.citation_verifier.verify_citations_batch que orquesta
chain cache → BD → live fetch a fuentes oficiales.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# Regex idéntico al frontend para detectar citas en cualquier prosa.
# Soporta variantes: T-329/1997, T-329/97, T-329 de 1997, "Sent. T-329/1997"
_SENTENCIA_RE = re.compile(
    r"\b(SU|SL|SC|SP|T|C)[-\s]?(\d{1,5})[/\-\s](?:de\s+)?(\d{2,4})\b",
    re.IGNORECASE,
)
_LEY_RE = re.compile(
    r"\bley\s+(\d{1,5})\s+(?:de|del|\/)\s*(\d{2,4})\b",
    re.IGNORECASE,
)
_DECRETO_RE = re.compile(
    r"\bdecreto(?:\s+(?:ley|reglamentario))?\s+(\d{1,5})\s+(?:de|del|\/)\s*(\d{2,4})\b",
    re.IGNORECASE,
)


def _normalize_year(yy: str) -> str:
    if len(yy) == 4:
        return yy
    n = int(yy)
    return ("20" if n < 50 else "19") + yy.zfill(2)


def _extract_citations(text: str) -> list[str]:
    """Devuelve la lista de citas canónicas detectadas en el texto."""
    if not text:
        return []
    refs: list[str] = []
    seen: set[str] = set()

    for m in _SENTENCIA_RE.finditer(text):
        tipo = m.group(1).upper()
        numero = m.group(2)
        anio = _normalize_year(m.group(3))
        ref = f"{tipo}-{numero}/{anio}"
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)

    for m in _LEY_RE.finditer(text):
        numero = m.group(1)
        anio = _normalize_year(m.group(2))
        ref = f"Ley {numero}/{anio}"
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)

    for m in _DECRETO_RE.finditer(text):
        numero = m.group(1)
        anio = _normalize_year(m.group(2))
        ref = f"Decreto {numero}/{anio}"
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)

    return refs


def _collect_text(output: Any) -> str:
    """Junta todo el texto del output del skill (dict o string)."""
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        parts: list[str] = []
        for k in ("text", "draft", "content", "summary"):
            v = output.get(k)
            if isinstance(v, str):
                parts.append(v)
        # Review skills (clauses)
        clauses = output.get("clauses")
        if isinstance(clauses, list):
            for c in clauses:
                if isinstance(c, dict):
                    for ck in ("text", "reason", "suggested_text"):
                        cv = c.get(ck)
                        if isinstance(cv, str):
                            parts.append(cv)
        return "\n".join(parts)
    return ""


async def run(context: dict[str, Any], config: Optional[dict] = None) -> Optional[dict]:
    """Verifica las citas del output del skill contra fuentes oficiales.

    Args:
        context: { output, input, firm_id, user_id, matter_id, ... }
        config: configuración del hook desde BD (jsonb)

    Returns:
        None si no hay citas en el output
        {decision: 'warn', reason, additional_context} si hay sospechosas
        {decision: 'approve'} si todas verifican OK
    """
    text = _collect_text(context.get("output"))
    refs = _extract_citations(text)
    if not refs:
        return None

    try:
        from utils.db import get_storage
        from utils.citation_verifier import verify_citations_batch
    except Exception as e:
        # Si no podemos cargar el verifier, no bloqueamos el skill
        return {"decision": "approve", "reason": f"verifier unavailable: {e}"}

    try:
        storage = await get_storage()
        if not hasattr(storage, "pool"):
            return {"decision": "approve", "reason": "storage_unavailable"}
        results = await verify_citations_batch(
            storage.pool,
            refs,
            firm_id=context.get("firm_id"),
            user_id=context.get("user_id"),
        )
    except Exception as e:
        return {"decision": "approve", "reason": f"verify_batch_error: {str(e)[:80]}"}

    sospechosas = [r for r in results if r.estado == "sospechosa"]
    derogadas = [r for r in results if r.estado in ("superada", "derogada")]
    verificadas = [r for r in results if r.estado == "verificada"]

    if not sospechosas and not derogadas:
        return {
            "decision": "approve",
            "reason": f"{len(verificadas)}/{len(refs)} citas verificadas en vivo",
            "additional_context": {
                "verificadas": [r.citation_ref for r in verificadas],
                "total": len(refs),
            },
        }

    # Hay problemas · construir warning detallado
    parts = []
    if sospechosas:
        parts.append(
            f"{len(sospechosas)} cita(s) NO encontrada(s) en fuente oficial "
            f"(posible alucinación): {', '.join(r.citation_ref for r in sospechosas[:10])}"
        )
    if derogadas:
        parts.append(
            f"{len(derogadas)} cita(s) derogada(s)/superada(s): "
            f"{', '.join(r.citation_ref for r in derogadas[:10])}"
        )

    return {
        "decision": "warn",
        "reason": " | ".join(parts),
        "additional_context": {
            "sospechosas": [r.citation_ref for r in sospechosas],
            "derogadas": [r.citation_ref for r in derogadas],
            "verificadas": [r.citation_ref for r in verificadas],
            "total": len(refs),
        },
    }
