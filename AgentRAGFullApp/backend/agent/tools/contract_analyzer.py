"""Sprint 11 · Contract Analyzer.

Toma un matter_document y extrae con LLM gpt-4o (JSON mode):
  - tipo de contrato
  - partes (con tax_id/personal_id)
  - cláusulas categorizadas (objeto, plazo, precio, penalidades, etc.)
  - riesgos detectados con severidad + texto sugerido
  - fechas/montos clave
  - risk_score 0-100

Reutiliza el lector de documentos del Sprint F1 (document_analysis.py) para
obtener texto del documento (Docling/resumen_ia/chunks).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


_SCHEMA = """Estructura JSON de salida (estricta):

{
  "contract_type": "arrendamiento|prestacion_servicios|laboral|compraventa|mandato|distribucion|confidencialidad|transaccion|otro",
  "parties": [
    {"rol": "arrendador|arrendatario|contratante|contratista|empleador|empleado|vendedor|comprador|mandante|mandatario|otro",
     "nombre": "string", "tax_id": "NIT 900.000.000-0 o null", "personal_id": "C.C. 12.345.678 o null"}
  ],
  "resumen_ejecutivo": "3-5 lineas en espanol",
  "fecha_inicio": "YYYY-MM-DD o null",
  "fecha_fin": "YYYY-MM-DD o null",
  "monto_total_cop": numero o null,
  "moneda": "COP|USD|EUR",
  "jurisdiccion": "string o null",
  "ley_aplicable": "string o null",
  "clauses": [
    {
      "category": "objeto|plazo|precio|penalidades|indemnidad|terminacion|jurisdiccion|confidencialidad|no_competencia|fuerza_mayor|garantias|otro",
      "numero": "string (ej. 'Clausula Quinta') o null",
      "titulo": "string corto",
      "texto": "string · transcripcion fiel · max 2000 chars",
      "importance": "critica|alta|normal|baja",
      "position": numero entero (orden en doc)
    }
  ],
  "risks": [
    {
      "kind": "clausula_abusiva|ambiguedad|faltante|desbalance|penalidad_excesiva|jurisdiccion_remota|indemnidad_unilateral|otro",
      "severity": "bajo|medio|alto|critico",
      "title": "string corto",
      "description": "string · por que es riesgo · 2-3 lineas",
      "suggested_action": "string corto · que hacer",
      "suggested_text": "string · redaccion alternativa o null",
      "clause_position": numero entero (referencia a clauses[].position) o null,
      "citations": ["CST Art. 64", "CGP Art. 590"]
    }
  ],
  "risk_score": entero 0-100
}

REGLAS:
- NO inventes datos. Si no esta en el texto, omite.
- Las fechas en ISO YYYY-MM-DD.
- Identifica riesgos colombianos especificos:
  * Clausula penal > 30% del valor total → penalidad_excesiva
  * Renuncia a fuero domiciliario en consumidor → clausula_abusiva
  * Indemnidad unilateral sin reciprocidad → indemnidad_unilateral
  * Jurisdiccion arbitral en ciudad distinta al domicilio del consumidor → jurisdiccion_remota
  * Falta de clausula de fuerza mayor en contratos largos → faltante
  * Plazo indefinido sin causa de terminacion → ambiguedad
- risk_score = ponderado: critico=25, alto=15, medio=8, bajo=3 (capped a 100)
- Si el documento NO es un contrato, devuelve contract_type='otro' y deja arrays vacios.
"""

_SYSTEM_PROMPT = (
    "Eres un abogado contractualista colombiano senior. Tu trabajo es analizar "
    "contratos y producir un reporte estructurado que un abogado titulado leeria "
    "antes de aconsejar a su cliente. Tu prioridad es detectar riesgos legales "
    "y proponer redlining cuando aplique. Idioma: espanol de Colombia.\n\n" + _SCHEMA
)


async def analyze_contract_tool(args: dict, ctx: dict) -> dict:
    """Tool entrypoint.

    args:
      document_id: matter_documents.id
    """
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    document_id = args.get("document_id") or args.get("matter_document_id")
    if not (firm_id and document_id):
        return {"error": "firm_id y document_id requeridos"}

    # 1. Resolver texto del documento
    text, pages = await _resolve_document_text(document_id, firm_id)
    if not text or len(text) < 80:
        return {
            "error": "texto_insuficiente",
            "detail": "El documento no tiene texto procesable (sube nuevamente con OCR).",
        }

    # 2. Crear analysis pending para que UI pueda mostrar progreso
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}

    async with storage.pool.acquire() as conn:
        matter_id = await conn.fetchval(
            "select matter_id from matter_documents where id = $1::uuid", document_id,
        )
        analysis = await conn.fetchrow(
            """
            insert into contract_analyses
              (firm_id, matter_document_id, matter_id, status, llm_model, created_by)
            values ($1::uuid, $2::uuid, $3::uuid, 'analyzing', 'gpt-4o', $4::uuid)
            returning id
            """,
            firm_id, document_id, matter_id, user_id,
        )
        analysis_id = analysis["id"]

    # 3. LLM call con JSON mode
    parsed: dict = {}
    prompt_tokens, completion_tokens = 0, 0
    try:
        from utils.llm import get_openai_client
        client = get_openai_client()
        # Limit texto a primeras ~30K chars (≈8K tokens) para no exceder ventana
        sample = text[:30000]
        resp = await client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"Analiza el siguiente contrato:\n\n{sample}"},
            ],
            temperature=0.2,
            max_tokens=4000,
        )
        parsed = json.loads(resp.choices[0].message.content or "{}")
        prompt_tokens = resp.usage.prompt_tokens if resp.usage else 0
        completion_tokens = resp.usage.completion_tokens if resp.usage else 0
    except Exception as e:
        logger.exception("contract_analyzer LLM failed")
        async with storage.pool.acquire() as conn:
            await conn.execute(
                "update contract_analyses set status = 'failed', metadata = $2::jsonb where id = $1::uuid",
                analysis_id, json.dumps({"error": str(e)[:500]}),
            )
        return {"error": "llm_failed", "detail": str(e)[:200]}

    # 4. Persist resultados
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            update contract_analyses set
              status = 'completed',
              contract_type = $2, parties = $3::jsonb,
              resumen_ejecutivo = $4,
              fecha_inicio = case when $5::text ~ '^\\d{4}-\\d{2}-\\d{2}$' then $5::date else null end,
              fecha_fin    = case when $6::text ~ '^\\d{4}-\\d{2}-\\d{2}$' then $6::date else null end,
              monto_total_cop = $7, moneda = coalesce($8, 'COP'),
              jurisdiccion = $9, ley_aplicable = $10,
              risk_score = least(coalesce($11, 0), 100),
              prompt_tokens = $12, completion_tokens = $13,
              metadata = $14::jsonb,
              updated_at = now()
             where id = $1::uuid
            """,
            analysis_id,
            parsed.get("contract_type"),
            json.dumps(parsed.get("parties") or []),
            parsed.get("resumen_ejecutivo"),
            parsed.get("fecha_inicio"), parsed.get("fecha_fin"),
            parsed.get("monto_total_cop"), parsed.get("moneda"),
            parsed.get("jurisdiccion"), parsed.get("ley_aplicable"),
            parsed.get("risk_score") or 0,
            prompt_tokens, completion_tokens,
            json.dumps({"pages": pages, "text_chars": len(text)}),
        )

        clause_id_by_position: dict[int, str] = {}
        for idx, c in enumerate(parsed.get("clauses") or []):
            pos = c.get("position") or idx
            row = await conn.fetchrow(
                """
                insert into contract_clauses
                  (firm_id, analysis_id, category, numero, titulo, texto, importance, position)
                values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8)
                returning id
                """,
                firm_id, analysis_id, c.get("category", "otro"),
                c.get("numero"), c.get("titulo"), (c.get("texto") or "")[:8000],
                c.get("importance", "normal"), pos,
            )
            clause_id_by_position[int(pos)] = str(row["id"])

        for r in parsed.get("risks") or []:
            cp = r.get("clause_position")
            clause_id = clause_id_by_position.get(int(cp)) if cp is not None else None
            try:
                await conn.execute(
                    """
                    insert into contract_risks
                      (firm_id, analysis_id, clause_id, kind, severity, title,
                       description, suggested_action, suggested_text, citations)
                    values ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7, $8, $9, $10::jsonb)
                    """,
                    firm_id, analysis_id, clause_id,
                    r.get("kind", "otro"), r.get("severity", "medio"),
                    r.get("title") or "Riesgo",
                    r.get("description") or "",
                    r.get("suggested_action"), r.get("suggested_text"),
                    json.dumps(r.get("citations") or []),
                )
            except Exception as e:
                logger.debug("risk insert skipped: %s", e)

    return {
        "analysis_id": str(analysis_id),
        "status": "completed",
        "contract_type": parsed.get("contract_type"),
        "risk_score": parsed.get("risk_score"),
        "clauses_count": len(parsed.get("clauses") or []),
        "risks_count": len(parsed.get("risks") or []),
    }


async def _resolve_document_text(document_id: str, firm_id: str) -> tuple[str, int]:
    """Mismo fallback que document_analysis.py: resumen_ia → chunks → vacio."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return "", 0
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            "select resumen_ia, pages, ingest_doc_id from matter_documents where id = $1::uuid and firm_id = $2::uuid",
            document_id, firm_id,
        )
        if not row:
            return "", 0
        if row["resumen_ia"] and len(row["resumen_ia"]) > 200:
            return row["resumen_ia"], row["pages"] or 0
        if row["ingest_doc_id"]:
            chunks = await conn.fetch(
                """
                select content from chunks
                 where document_id = $1::text
                 order by chunk_index asc limit 60
                """,
                str(row["ingest_doc_id"]),
            )
            text = "\n\n".join(c["content"] for c in chunks if c["content"])
            if text:
                return text, row["pages"] or 0
    return "", 0
