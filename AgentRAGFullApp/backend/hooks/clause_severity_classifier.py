"""Hook · clasifica cláusulas con severity GREEN/YELLOW/RED si el output tiene clauses."""

from __future__ import annotations

from typing import Any, Optional

RED_KEYWORDS = [
    "ilimitada", "ilimitado", "sin restricción", "renuncia total",
    "exclusividad perpetua", "non-compete perpetuo",
    "cesión total propiedad intelectual sin contraprestación",
]

YELLOW_KEYWORDS = [
    "exclusividad", "indemnización", "penalidad", "automática",
    "renovación tácita", "rescisión unilateral",
]


async def run(context: dict[str, Any], config: Optional[dict] = None) -> Optional[dict]:
    """Si output tiene 'clauses' sin severity, intenta clasificar con heurística rápida."""
    out = context.get("output") or {}
    if not isinstance(out, dict):
        return None
    clauses = out.get("clauses")
    if not isinstance(clauses, list) or not clauses:
        return None

    classified = 0
    counts = {"green": 0, "yellow": 0, "red": 0}

    for c in clauses:
        if not isinstance(c, dict):
            continue
        if c.get("severity") in ("green", "yellow", "red"):
            counts[c["severity"]] += 1
            continue
        text = (
            str(c.get("texto") or c.get("text") or "")
            + " " + str(c.get("titulo") or "")
        ).lower()
        if not text.strip():
            continue
        severity = "green"
        if any(k in text for k in RED_KEYWORDS):
            severity = "red"
        elif any(k in text for k in YELLOW_KEYWORDS):
            severity = "yellow"
        c["severity"] = severity
        c["severity_source"] = "heuristic"
        counts[severity] += 1
        classified += 1

    out["clauses"] = clauses
    out["severity_summary"] = counts

    return {
        "decision": "approve",
        "reason": f"Clasificadas {classified} cláusulas (heuristic)",
        "additional_context": f"green={counts['green']} yellow={counts['yellow']} red={counts['red']}",
    }
