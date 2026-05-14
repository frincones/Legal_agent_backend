"""Sprint 21 · Inconsistency detector · LLM analiza un documento.

Detecta:
  · Fechas contradictorias (firmado 2025 pero ley citada de 2027)
  · Nombres que cambian entre páginas
  · Montos que no cuadran (subtotal vs total)
  · Sellos o firmas mencionados pero ausentes
  · Citas a normas inexistentes (heurística)
  · Referencias cruzadas rotas
  · Identificaciones (cédula/NIT) que no cuadran con nombre

Devuelve JSON estricto · cache por document_hash 24h.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """Eres un abogado litigante senior colombiano especializado en
detectar inconsistencias en evidencia documental antes de presentarla al juez.

Analizas el texto de un documento (PDF, contrato, escrito, certificado) y
detectas:

  1. Fechas contradictorias · ej: "firmado el 15 de marzo de 2024" pero
     "según la Ley 2295 de 2025" (la ley aún no existía).
  2. Nombres o identificaciones que NO coinciden entre secciones.
  3. Montos que no cuadran · subtotal vs total, multiplicación incorrecta.
  4. Firmas o sellos mencionados ("firma del representante legal") pero
     no presentes en el texto.
  5. Referencias a normas/sentencias que parecen inventadas.
  6. Referencias internas rotas · "ver cláusula 7" pero no hay cláusula 7.
  7. Faltantes obvios (un certificado sin fecha, un contrato sin partes).

Devuelve UN solo objeto JSON con esta estructura estricta:

{
  "inconsistencies": [
    {
      "type": "fecha_contradictoria" | "nombre_no_coincide" | "monto_incorrecto" |
              "firma_faltante" | "sello_faltante" | "norma_inexistente" |
              "referencia_rota" | "campo_faltante" | "otro",
      "severity": "high" | "medium" | "low",
      "location": "ubicación textual o nombre de cláusula/página",
      "description": "explicación clara y específica · 1-2 líneas",
      "suggestion": "qué corregir / verificar"
    }
  ],
  "total_count": número total,
  "high_severity_count": cuántas son severity=high,
  "summary": "1-2 líneas resumiendo el estado del documento"
}

Reglas estrictas:
- NO inventes inconsistencias. Si no detectas ninguna, devuelve total_count=0
  y `inconsistencies` array vacío con summary positivo.
- Sé específico · "Fecha del contrato (2024-03-15) anterior a la fecha de
  vigencia de la Ley 2295 de 2025 (no existe)" es mejor que "fecha inválida".
- Severity HIGH solo para problemas que harán fallar la evidencia.
- MEDIUM para puntos que el juez probablemente cuestionará.
- LOW para detalles formales menores.
- Máx 15 inconsistencias en total · prioriza las HIGH.
"""


async def detect_inconsistencies_in_document(
    firm_id: str,
    matter_document_id: str,
    document_text: str,
    matter_id: Optional[str] = None,
    user_id: Optional[str] = None,
    use_cache: bool = True,
) -> dict:
    """Analiza un documento + persiste el resultado."""
    from utils.db import get_storage
    from utils.llm import llm_generate_json
    from utils.evidence_helpers import hash_document_text

    if not document_text or len(document_text.strip()) < 100:
        raise ValueError("Documento muy corto (mín 100 caracteres)")

    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise ValueError("Storage no disponible")

    doc_hash = hash_document_text(document_text)

    # Cache hit
    if use_cache:
        async with storage.pool.acquire() as conn:
            cached = await conn.fetchrow(
                """
                select id, inconsistencies, total_count, high_severity_count,
                       summary, analyzed_at, model_used
                  from evidence_inconsistencies
                 where firm_id = $1::uuid and matter_document_id = $2::uuid
                   and document_hash = $3
                   and analyzed_at > now() - interval '24 hours'
                 order by analyzed_at desc limit 1
                """,
                firm_id, matter_document_id, doc_hash,
            )
            if cached:
                return _serialize_inc(cached, cached=True)

    # Truncar texto demasiado largo
    text = document_text.strip()
    if len(text) > 30_000:
        # Para docs muy largos, tomamos head + tail
        text = text[:18_000] + "\n…\n" + text[-12_000:]

    user_prompt = (
        "Analiza el siguiente documento y detecta inconsistencias en JSON estricto.\n\n"
        + "# Documento\n" + text
    )

    try:
        parsed = await llm_generate_json(
            prompt=user_prompt,
            model="gpt-4o-mini",
            system_prompt=SYSTEM_PROMPT,
            temperature=0.0,
            max_tokens=2000,
            purpose="inconsistency_detect",
            session_id=str(user_id) if user_id else "",
        )
    except Exception as e:
        logger.exception("inconsistency LLM failed")
        raise ValueError(f"LLM falló: {e}")

    inconsistencies = _coerce_inconsistencies(parsed.get("inconsistencies"))
    total = len(inconsistencies)
    high = sum(1 for x in inconsistencies if x.get("severity") == "high")
    summary = str(parsed.get("summary") or "").strip()
    if not summary:
        summary = "Documento analizado."

    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into evidence_inconsistencies
              (firm_id, matter_id, matter_document_id, document_hash,
               inconsistencies, total_count, high_severity_count, summary,
               model_used, analyzed_by)
            values ($1::uuid, $2::uuid, $3::uuid, $4, $5::jsonb, $6, $7, $8,
                    'gpt-4o-mini', $9::uuid)
            returning id, analyzed_at
            """,
            firm_id, matter_id, matter_document_id, doc_hash,
            json.dumps(inconsistencies), total, high, summary[:1000],
            user_id,
        )

    return {
        "id": str(row["id"]),
        "matter_document_id": matter_document_id,
        "analyzed_at": row["analyzed_at"].isoformat() if row["analyzed_at"] else None,
        "inconsistencies": inconsistencies,
        "total_count": total,
        "high_severity_count": high,
        "summary": summary,
        "cached": False,
    }


VALID_TYPES = {
    "fecha_contradictoria", "nombre_no_coincide", "monto_incorrecto",
    "firma_faltante", "sello_faltante", "norma_inexistente",
    "referencia_rota", "campo_faltante", "otro",
}
VALID_SEVERITY = {"high", "medium", "low"}


def _coerce_inconsistencies(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for x in raw[:15]:
        if not isinstance(x, dict):
            continue
        t = (x.get("type") or "otro").strip().lower()
        if t not in VALID_TYPES:
            t = "otro"
        sev = (x.get("severity") or "medium").strip().lower()
        if sev not in VALID_SEVERITY:
            sev = "medium"
        out.append({
            "type": t,
            "severity": sev,
            "location": str(x.get("location") or "")[:120],
            "description": str(x.get("description") or "")[:500],
            "suggestion": str(x.get("suggestion") or "")[:300],
        })
    return out


def _serialize_inc(r, cached: bool = False) -> dict:
    incs = r["inconsistencies"]
    if isinstance(incs, str):
        try:
            incs = json.loads(incs)
        except Exception:
            incs = []
    return {
        "id": str(r["id"]),
        "analyzed_at": r["analyzed_at"].isoformat() if r["analyzed_at"] else None,
        "inconsistencies": incs or [],
        "total_count": int(r["total_count"] or 0),
        "high_severity_count": int(r["high_severity_count"] or 0),
        "summary": r["summary"],
        "cached": cached,
    }
