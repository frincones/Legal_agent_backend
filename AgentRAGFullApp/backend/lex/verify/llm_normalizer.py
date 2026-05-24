"""Sprint M15 · LLM Normalizer — último recurso para texto libre.

Solo se invoca cuando:
1. `expand_to_canonical()` (M13) NO encuentra alias
2. `parse_citation_ref()` retorna None
3. Pero el texto SÍ parece ser una cita legal (heurística)

Usa gpt-4o-mini con function calling. Costo ~$0.0003 por call.
Cache TTL 24h en external_fetch_cache.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# Function schema para gpt-4o-mini
NORMALIZE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "extract_legal_citation",
        "description": "Extrae componentes estructurados de una cita legal colombiana",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["jurisprudencia", "ley", "decreto", "codigo", "codigo_articulo", "resolucion", "circular", "unknown"],
                    "description": "Tipo de norma",
                },
                "tipo": {
                    "type": "string",
                    "description": "Subtipo (T, C, SU, SL, SC, SP, CST, CGP, CONSTITUCION, LEY, DECRETO, etc.)",
                },
                "numero": {
                    "type": "integer",
                    "description": "Número de la norma (ej. 64 para Art. 64 CST; 1430 para SL1430-2022)",
                },
                "anio": {
                    "type": "integer",
                    "description": "Año (4 dígitos preferido, ej. 2022). Null si es un código sin año.",
                },
                "fuente_inferida": {
                    "type": "string",
                    "enum": ["CORTE_CONSTITUCIONAL", "CORTE_SUPREMA", "CONSEJO_ESTADO", "SENADO", "CONGRESO", "GOBIERNO_NACIONAL", "MINISTERIO", "DESCONOCIDO"],
                },
                "confidence": {
                    "type": "number",
                    "description": "0.0-1.0 — qué tan seguro está el modelo de la extracción",
                },
            },
            "required": ["kind", "confidence"],
        },
    },
}


SYSTEM_PROMPT = """Eres un parser de citas legales colombianas. Tu única tarea es
extraer componentes estructurados (kind, tipo, numero, anio, fuente) de una cita
en texto libre. NO inventes datos. Si el texto NO es una cita legal, retorna
kind='unknown' con confidence=0.0.

Ejemplos:
- "Art. 64 CST" → {kind:"codigo_articulo", tipo:"CST", numero:64, anio:null}
- "Sentencia T-760 de 2008" → {kind:"jurisprudencia", tipo:"T", numero:760, anio:2008}
- "el SL 1430 del 2022" → {kind:"jurisprudencia", tipo:"SL", numero:1430, anio:2022}
- "estatuto del consumidor" → {kind:"unknown", confidence:0.0}
- "Ley 50 del 90" → {kind:"ley", tipo:"LEY", numero:50, anio:1990}
"""


async def llm_normalize_citation(
    raw: str,
    client,
    pool=None,
) -> Optional[dict]:
    """Intenta parsear texto libre como cita legal usando gpt-4o-mini.

    Returns:
        dict con {kind, tipo, numero, anio, fuente_inferida, confidence}
        o None si el LLM no pudo extraer o falló.

    Cache TTL 24h en external_fetch_cache.
    """
    if not raw or len(raw.strip()) < 3:
        return None

    cache_key = f"llm_norm:{hashlib.sha256(raw.lower().strip().encode()).hexdigest()[:32]}"

    # Cache check
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT content_jsonb FROM external_fetch_cache
                    WHERE cache_key = $1
                      AND fetched_at + (ttl_seconds || ' seconds')::interval > now()
                    """,
                    cache_key,
                )
            if row and row["content_jsonb"]:
                cached = row["content_jsonb"]
                if isinstance(cached, str):
                    cached = json.loads(cached)
                logger.debug("llm_normalize cache hit for %r", raw[:60])
                return cached
        except Exception as e:
            logger.warning("llm_normalize cache_get failed: %s", e)

    # LLM call con function calling
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Cita: {raw[:500]}"},
            ],
            tools=[NORMALIZE_TOOL_SCHEMA],
            tool_choice={"type": "function", "function": {"name": "extract_legal_citation"}},
            temperature=0.0,
            max_tokens=300,
        )
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return None
        args_str = msg.tool_calls[0].function.arguments
        result = json.loads(args_str)

        # Validar
        if result.get("kind") == "unknown" or result.get("confidence", 0) < 0.5:
            normalized = None
        else:
            normalized = {
                "kind": result.get("kind"),
                "tipo": result.get("tipo"),
                "numero": result.get("numero"),
                "anio": result.get("anio"),
                "fuente_inferida": result.get("fuente_inferida"),
                "confidence": result.get("confidence", 0.0),
            }

        # Cache (incluso si normalized=None, evitar re-llamadas para misma cita)
        if pool is not None:
            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO external_fetch_cache
                          (cache_key, source, content_jsonb, status, ttl_seconds)
                        VALUES ($1, 'llm_normalizer', $2::jsonb, 'ok', 86400)
                        ON CONFLICT (cache_key) DO UPDATE
                          SET content_jsonb = EXCLUDED.content_jsonb,
                              fetched_at = now()
                        """,
                        cache_key,
                        json.dumps(normalized or {"kind": "unknown"}, ensure_ascii=False),
                    )
            except Exception as e:
                logger.warning("llm_normalize cache_set failed: %s", e)

        return normalized
    except Exception as e:
        logger.warning("llm_normalize_citation failed for %r: %s", raw[:60], e)
        return None
