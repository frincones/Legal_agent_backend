"""Hook · bloquea redlines/drafts con términos del playbook forbidden_terms."""

from __future__ import annotations

from typing import Any, Optional


async def run(context: dict[str, Any], config: Optional[dict] = None) -> Optional[dict]:
    playbook = context.get("playbook") or {}
    forbidden = playbook.get("forbidden_terms") or []
    if not forbidden:
        return None

    text_sources = []
    out = context.get("output") or {}
    if isinstance(out, dict):
        for k in ("text", "draft", "content", "redline_text"):
            v = out.get(k)
            if isinstance(v, str):
                text_sources.append(v)
        redlines = out.get("redlines") or []
        if isinstance(redlines, list):
            for r in redlines:
                if isinstance(r, dict) and r.get("suggested"):
                    text_sources.append(str(r["suggested"]))
    full = "\n".join(text_sources).lower()
    if not full:
        return None

    hits = []
    for term in forbidden:
        if not term:
            continue
        t = term.lower().strip()
        if t and t in full:
            hits.append(term)

    if hits:
        return {
            "decision": "block",
            "reason": f"El draft contiene términos prohibidos por playbook firm: {', '.join(hits)}",
            "additional_context": "Edita el playbook en /settings/playbook para ajustar reglas.",
        }
    return {"decision": "approve"}
