"""Hook · verifica cláusula Ley 1581/2012 (Habeas Data) en contratos con datos personales."""

from __future__ import annotations

from typing import Any, Optional

HABEAS_DATA_KEYWORDS = [
    "habeas data", "ley 1581", "tratamiento de datos",
    "decreto 1377", "tratamiento información personal",
    "autorización tratamiento", "encargado del tratamiento",
]

PERSONAL_DATA_TRIGGERS = [
    "datos personales", "información personal", "datos del titular",
    "cédula", "documento identidad", "correo electrónico",
    "número de teléfono", "dirección residencia",
]


async def run(context: dict[str, Any], config: Optional[dict] = None) -> Optional[dict]:
    """Si el contrato menciona datos personales → debe incluir cláusula Habeas Data."""
    text_sources = []

    out = context.get("output") or {}
    if isinstance(out, dict):
        for k in ("text", "draft", "content", "redline_text"):
            v = out.get(k)
            if isinstance(v, str):
                text_sources.append(v)
        clauses = out.get("clauses") or []
        if isinstance(clauses, list):
            for c in clauses:
                if isinstance(c, dict):
                    text_sources.append(str(c.get("texto") or c.get("text") or ""))

    inp = context.get("input") or {}
    if isinstance(inp, dict):
        for k in ("document_text", "prompt", "content"):
            v = inp.get(k)
            if isinstance(v, str):
                text_sources.append(v)

    full_text = "\n".join(text_sources).lower()
    if not full_text:
        return None

    has_personal_data = any(t in full_text for t in PERSONAL_DATA_TRIGGERS)
    if not has_personal_data:
        return None

    has_habeas_clause = any(k in full_text for k in HABEAS_DATA_KEYWORDS)
    if has_habeas_clause:
        return {"decision": "approve"}

    return {
        "decision": "warn",
        "reason": "El contrato menciona datos personales pero no incluye cláusula explícita de Habeas Data (Ley 1581/2012).",
        "additional_context": (
            "Sugerido: insertar cláusula con (a) finalidad del tratamiento, "
            "(b) datos del responsable y encargado, (c) derechos del titular (ARCO), "
            "(d) procedimientos para revocar autorización. Plantilla: /redactar/clausula-habeas-data"
        ),
    }
