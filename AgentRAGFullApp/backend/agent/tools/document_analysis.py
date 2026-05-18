"""F1 · Document deep analysis · entity extraction.

Wraps the existing Docling reader + LLM JSON-mode call to extract
structured legal entities from a `matter_documents` row:
  - parties (rol, nombre, tax_id)
  - dates (firma, vencimiento, audiencia, exigibilidad)
  - obligations (deudor, acreedor, monto, vencimiento)
  - montos (concepto, monto, moneda)
  - inconsistencies (cosas raras detectadas)
  - hechos_clave (resumen narrativo en 5-10 líneas)
  - riesgos_legales (qué se ve riesgoso)
  - vacios_probatorios (lo que falta para probar el caso)

Outputs a row to `document_extractions`. If the document already had
an extraction, the new row links via `prev_extraction_id` (versioning).

Side effect (best-effort, non-blocking): inserta filas faltantes en
`matter_parties` con `origen='ai_extracted'`. Si una parte ya existe
(match por nombre+tax_id), no se duplica.

The tool is callable from voice agent AND from REST endpoint.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ─── Schema enviado al LLM en el system prompt ─────────────────────────

_SCHEMA_INSTRUCTIONS = """Estructura del JSON de salida:

{
  "parties": [{
    "rol": "demandante|demandado|tercero|interviniente|representante|otro",
    "nombre": "string",
    "tax_id": "NIT 900.123.456-7 (o null)",
    "personal_id": "C.C. 41.123.456 (o null)",
    "confianza": 0.0-1.0
  }],
  "dates": [{
    "tipo": "firma|exigibilidad|vencimiento|audiencia|notificacion|hechos|otro",
    "fecha": "YYYY-MM-DD",
    "descripcion": "string corta",
    "confianza": 0.0-1.0
  }],
  "obligations": [{
    "deudor": "string",
    "acreedor": "string",
    "descripcion": "string",
    "monto_cop": null o número,
    "vencimiento": "YYYY-MM-DD o null",
    "confianza": 0.0-1.0
  }],
  "montos": [{
    "concepto": "string",
    "monto": número,
    "moneda": "COP|USD|EUR",
    "confianza": 0.0-1.0
  }],
  "inconsistencies": [{
    "descripcion": "qué es inconsistente y por qué",
    "severidad": "baja|media|alta"
  }],
  "hechos_clave": "resumen narrativo de 5 a 10 líneas",
  "riesgos_legales": [{
    "riesgo": "string",
    "severidad": "baja|media|alta",
    "fundamento": "norma o doctrina aplicable"
  }],
  "vacios_probatorios": [{
    "descripcion": "qué falta para probar tal hecho",
    "sugerencia": "qué documento o testigo se necesita"
  }],
  "confidence_score": 0.0-1.0
}

REGLAS:
- Si el documento NO contiene cierta información, devuelve array vacío.
- NO inventes datos. Si un dato no aparece textualmente, omítelo.
- Las fechas deben ser ISO 8601 (YYYY-MM-DD). Si la fecha es relativa
  ("hace 6 meses") y no se puede inferir absoluta, omítela.
- "tax_id" debe normalizarse a "NIT 900.000.000-0".
- "personal_id" debe normalizarse a "C.C. 12.345.678".
- Para Colombia: roles de demandante/demandado son los del CGP/CST.
- Si encuentras texto en otro idioma o sin sentido legal, marca
  inconsistencies con severidad 'alta'.
- "confidence_score" global refleja qué tan completa fue la extracción
  (1.0 = todos los campos identificados claramente)."""


_SYSTEM_PROMPT = (
    "Eres un paralegal jurídico colombiano experto en lectura crítica de "
    "documentos legales. Extraes información estructurada con la precisión "
    "de un abogado titulado. NO inventas datos. NO interpretas más allá del "
    "texto. Idioma: español de Colombia.\n\n"
    + _SCHEMA_INSTRUCTIONS
)


async def _read_document_text(
    document_id: str,
    firm_id: str,
    storage_path: Optional[str],
) -> tuple[str, int]:
    """Resuelve el texto del documento con fallback en cadena:

      1. Si `matter_documents.resumen_ia` tiene >100 chars → úsalo (modo demo)
      2. Si hay `chunks` ingestados linkados al documento → concatenar
      3. Si `storage_path` resolvable → descarga via httpx a Supabase Storage
         y procesa con Docling (cuando esté disponible).
      4. Sino → string vacío.

    Devuelve (texto, pages_count). pages_count=0 si desconocido.
    """
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return "", 0

    async with storage.pool.acquire() as conn:
        # 1. Resumen IA cacheado (siempre disponible en demo seed)
        row = await conn.fetchrow(
            "select resumen_ia, pages, ingest_doc_id "
            "from matter_documents where id = $1::uuid and firm_id = $2::uuid",
            document_id, firm_id,
        )
        if row and row["resumen_ia"] and len(row["resumen_ia"]) > 80:
            return row["resumen_ia"], row["pages"] or 0

        # 2. Chunks ingestados al RAG
        if row and row["ingest_doc_id"]:
            chunks = await conn.fetch(
                "select content from chunks where document_id = $1::uuid "
                "order by chunk_index limit 50",
                row["ingest_doc_id"],
            )
            if chunks:
                text = "\n\n".join(c["content"] for c in chunks if c["content"])
                if len(text) > 100:
                    return text, row["pages"] or 0

    # 3. Storage download (si Supabase env vars existen)
    if storage_path:
        try:
            import os
            import httpx
            sb_url = (
                os.getenv("SUPABASE_URL")
                or os.getenv("NEXT_PUBLIC_SUPABASE_URL")
            )
            sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
            if sb_url and sb_key:
                bucket = "matter-docs"
                url = f"{sb_url}/storage/v1/object/{bucket}/{storage_path}"
                async with httpx.AsyncClient(timeout=30) as client:
                    r = await client.get(
                        url,
                        headers={"authorization": f"Bearer {sb_key}", "apikey": sb_key},
                    )
                    if r.status_code == 200:
                        try:
                            from ingestion.readers.docling_reader import DoclingReader
                            reader = DoclingReader()
                            result = await reader.read_bytes(r.content, filename=storage_path)
                            text = (result.get("text") if isinstance(result, dict) else str(result)) or ""
                            pages = result.get("pages", 0) if isinstance(result, dict) else 0
                            if text:
                                return text, pages
                        except Exception as e:
                            logger.debug("Docling unavailable: %s", e)
                        # Last resort: best-effort UTF-8 decode
                        return r.content.decode("utf-8", errors="ignore"), 0
        except Exception as e:
            logger.debug("storage download failed: %s", e)

    return "", 0


async def _persist_extraction(
    firm_id: str,
    matter_id: Optional[str],
    matter_document_id: str,
    payload: dict,
    pages: int,
    model: str,
    prev_id: Optional[str] = None,
) -> str:
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise RuntimeError("storage pool no disponible")
    new_id = str(uuid.uuid4())
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            insert into document_extractions
              (id, firm_id, matter_id, matter_document_id, status,
               parties_jsonb, dates_jsonb, obligations_jsonb, montos_jsonb,
               inconsistencies_jsonb, hechos_clave, riesgos_legales,
               vacios_probatorios, confidence_score, model_used,
               prompt_version, pages_processed, prev_extraction_id)
            values
              ($1::uuid, $2::uuid, $3::uuid, $4::uuid, 'completed',
               $5::jsonb, $6::jsonb, $7::jsonb, $8::jsonb,
               $9::jsonb, $10, $11::jsonb,
               $12::jsonb, $13, $14,
               $15, $16, $17::uuid)
            """,
            new_id, firm_id, matter_id, matter_document_id,
            json.dumps(payload.get("parties", [])),
            json.dumps(payload.get("dates", [])),
            json.dumps(payload.get("obligations", [])),
            json.dumps(payload.get("montos", [])),
            json.dumps(payload.get("inconsistencies", [])),
            payload.get("hechos_clave"),
            json.dumps(payload.get("riesgos_legales", [])),
            json.dumps(payload.get("vacios_probatorios", [])),
            float(payload.get("confidence_score") or 0.0),
            model,
            "extract-v1",
            pages,
            prev_id,
        )
    return new_id


async def _autofill_matter_parties(
    firm_id: str,
    matter_id: str,
    parties: list[dict],
) -> int:
    """Inserta partes nuevas (no duplicadas) con origen='ai_extracted'.

    Match por (nombre lower) + (tax_id o personal_id). Best-effort.
    """
    if not (matter_id and parties):
        return 0
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return 0
    inserted = 0
    async with storage.pool.acquire() as conn:
        existing = await conn.fetch(
            "select lower(nombre) as nombre, tax_id, metadata "
            "from matter_parties where matter_id = $1::uuid and firm_id = $2::uuid",
            matter_id, firm_id,
        )
        existing_keys = set()
        for r in existing:
            existing_keys.add((r["nombre"], r["tax_id"]))
        for p in parties:
            nombre = (p.get("nombre") or "").strip()
            if len(nombre) < 2:
                continue
            tax_id = p.get("tax_id")
            key = (nombre.lower(), tax_id)
            if key in existing_keys:
                continue
            try:
                await conn.execute(
                    """
                    insert into matter_parties (matter_id, firm_id, rol, nombre, tax_id, origen, metadata)
                    values ($1::uuid, $2::uuid, $3, $4, $5, 'ai_extracted', $6::jsonb)
                    """,
                    matter_id, firm_id,
                    (p.get("rol") or "otro").lower(),
                    nombre,
                    tax_id,
                    json.dumps({"confianza": p.get("confianza"), "personal_id": p.get("personal_id")}),
                )
                inserted += 1
            except Exception as e:
                logger.debug("autofill matter_party skipped: %s", e)
                continue
    return inserted


async def extract_document_entities_tool(args: dict, ctx: dict) -> dict:
    """Voice / REST tool · extrae entidades de un documento.

    args:
      document_id: uuid del matter_documents (requerido)
      regenerate:  si true, ignora caché y re-extrae (default false)

    Returns:
      { id, status, parties_count, obligations_count, ... }
      o { error: "..." }
    """
    document_id = args.get("document_id")
    if not document_id:
        return {"error": "document_id requerido"}
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido (auth)"}
    regenerate = bool(args.get("regenerate") or False)

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}

    async with storage.pool.acquire() as conn:
        doc = await conn.fetchrow(
            """
            select id, matter_id, storage_path, byte_size, kind, status, ocr_done, titulo
            from matter_documents
            where id = $1::uuid and firm_id = $2::uuid
            """,
            document_id, firm_id,
        )
        if not doc:
            return {"error": "documento no encontrado"}

        if not regenerate:
            existing = await conn.fetchrow(
                """
                select id, status, confidence_score, extracted_at,
                       parties_jsonb, dates_jsonb, obligations_jsonb,
                       inconsistencies_jsonb, hechos_clave
                from document_extractions
                where matter_document_id = $1::uuid
                  and firm_id = $2::uuid
                  and status = 'completed'
                order by extracted_at desc
                limit 1
                """,
                document_id, firm_id,
            )
            if existing:
                return {
                    "id": str(existing["id"]),
                    "cached": True,
                    "status": "completed",
                    "confidence_score": float(existing["confidence_score"] or 0),
                    "parties_count": len(existing["parties_jsonb"] or []),
                    "obligations_count": len(existing["obligations_jsonb"] or []),
                    "inconsistencies_count": len(existing["inconsistencies_jsonb"] or []),
                    "extracted_at": existing["extracted_at"].isoformat(),
                    "hechos_clave": existing["hechos_clave"],
                }

    # Cold path · resolver texto desde resumen_ia, chunks o storage
    started = time.time()
    try:
        text, pages = await _read_document_text(
            document_id=document_id,
            firm_id=firm_id,
            storage_path=doc["storage_path"],
        )
    except Exception as e:
        return {"error": f"lectura del documento falló: {e}"}

    if not text or len(text.strip()) < 50:
        return {"error": "documento vacío o ilegible (OCR pendiente?)"}

    # Cap text para no quemar tokens
    if len(text) > 60_000:
        text = text[:60_000] + "\n[... documento truncado por longitud ...]"

    from utils.llm import llm_generate_json
    try:
        result = await llm_generate_json(
            prompt=(
                f"Documento '{doc['titulo'] or doc['kind']}'. Texto:\n\n{text}\n\n"
                "Extrae el JSON estructurado siguiendo el esquema."
            ),
            model="gpt-4o-mini",
            system_prompt=_SYSTEM_PROMPT,
            temperature=0.0,
            max_tokens=4000,
            purpose="document_extraction",
            session_id=ctx.get("session_id", ""),
        )
    except Exception as e:
        logger.exception("LLM extraction failed: %s", e)
        return {"error": f"extracción IA falló: {e}"}

    extraction_id = await _persist_extraction(
        firm_id=firm_id,
        matter_id=str(doc["matter_id"]) if doc["matter_id"] else None,
        matter_document_id=document_id,
        payload=result,
        pages=pages,
        model="gpt-4o-mini",
    )

    inserted = 0
    if doc["matter_id"]:
        inserted = await _autofill_matter_parties(
            firm_id=firm_id,
            matter_id=str(doc["matter_id"]),
            parties=result.get("parties", []),
        )

    duration_ms = int((time.time() - started) * 1000)
    from agent.tools._ui_events import ui_data_changed
    return {
        "id": extraction_id,
        "cached": False,
        "status": "completed",
        "confidence_score": float(result.get("confidence_score") or 0),
        "parties_count": len(result.get("parties", [])),
        "obligations_count": len(result.get("obligations", [])),
        "inconsistencies_count": len(result.get("inconsistencies", [])),
        "matter_parties_inserted": inserted,
        "duration_ms": duration_ms,
        "hechos_clave": result.get("hechos_clave"),
        "_ui_command": ui_data_changed(
            "documents", matter_id=str(doc["matter_id"]) if doc.get("matter_id") else None,
            firm_id=firm_id, op="update",
            extra={"document_id": str(document_id), "extraction_id": extraction_id,
                   "parties_inserted": inserted},
        ),
    }
