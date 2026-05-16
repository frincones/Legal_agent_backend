"""Hook · valida citas T-XXX/AAAA y SU-XXX/AAAA contra tabla jurisprudencia.

Reusa la lógica de api/citations.py · sólo verifica, no agrega.
"""

from __future__ import annotations

import re
from typing import Any, Optional

CITATION_REGEX = re.compile(
    r"\b(T|SU|C)[-\s]?([0-9]{2,4})\s*/?\s*([0-9]{2,4})\b",
    re.IGNORECASE,
)


async def run(context: dict[str, Any], config: Optional[dict] = None) -> Optional[dict]:
    """Extrae citas del output y valida existencia."""
    out = context.get("output") or {}
    text_sources = []
    if isinstance(out, dict):
        for k in ("text", "draft", "content"):
            v = out.get(k)
            if isinstance(v, str):
                text_sources.append(v)
    full = "\n".join(text_sources)
    if not full:
        return None

    found_refs = set()
    for match in CITATION_REGEX.finditer(full):
        kind = match.group(1).upper()
        num = match.group(2)
        year = match.group(3)
        if len(year) == 2:
            year = ("20" if int(year) < 50 else "19") + year
        found_refs.add(f"{kind}-{num}/{year}")

    if not found_refs:
        return None

    # Para evitar dependencias circulares, hacemos verify inline
    try:
        from utils.db import get_storage
        storage = await get_storage()
        unverified: list[str] = []
        async with storage.pool.acquire() as conn:
            for ref in list(found_refs)[:30]:
                row = await conn.fetchrow(
                    "select 1 from jurisprudencia where referencia ilike $1 limit 1",
                    f"%{ref}%",
                )
                if not row:
                    unverified.append(ref)
        if unverified:
            return {
                "decision": "warn",
                "reason": f"{len(unverified)} citas no verificadas en base local",
                "additional_context": "Sin verificar: " + ", ".join(unverified[:10]),
            }
    except Exception:
        return None
    return {"decision": "approve"}
