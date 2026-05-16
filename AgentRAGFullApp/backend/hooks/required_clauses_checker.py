"""Hook · verifica que required_clauses estén presentes en el contrato output."""

from __future__ import annotations

from typing import Any, Optional


async def run(context: dict[str, Any], config: Optional[dict] = None) -> Optional[dict]:
    playbook = context.get("playbook") or {}
    required = playbook.get("required_clauses") or []
    if not required:
        return None

    out = context.get("output") or {}
    text_sources = []
    if isinstance(out, dict):
        for k in ("text", "draft", "content"):
            v = out.get(k)
            if isinstance(v, str):
                text_sources.append(v)
        clauses = out.get("clauses") or []
        if isinstance(clauses, list):
            for c in clauses:
                if isinstance(c, dict):
                    text_sources.append(str(c.get("titulo") or ""))
                    text_sources.append(str(c.get("category") or ""))
                    text_sources.append(str(c.get("texto") or ""))
    full = "\n".join(text_sources).lower()
    if not full:
        return None

    missing = []
    for clause in required:
        if not clause:
            continue
        c = clause.lower().strip()
        if c and c not in full:
            missing.append(clause)

    if missing:
        return {
            "decision": "warn",
            "reason": f"Faltan cláusulas obligatorias del playbook: {', '.join(missing)}",
            "additional_context": "Agrega las cláusulas faltantes desde plantillas o ajusta el playbook.",
        }
    return {"decision": "approve"}
