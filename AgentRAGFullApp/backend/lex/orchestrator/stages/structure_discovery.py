"""M19.23.B — Structure Discovery Stage.

Reemplaza la lógica hardcoded de `_resolve_plan()` / `_resolve_template()`
del orchestrator con un mecanismo dinámico que:

  1. Construye una `structure_key` desde (doc_type, jurisdiccion,
     cuantia_rango, demandado_tipo) inferidos del enriched_context.
  2. Busca en cache `structure_recipes` table → 0s si hit.
  3. Si miss:
     a. LLM (gpt-4o-mini) genera plan inicial con conocimiento del
        CGP/CPACA/CPP/CST + reglas autoritativas (Art. 82 CGP requisitos,
        Art. 388 CGP divorcio, Art. 162 CPACA, etc.).
     b. (Opcional) VerificationAgent verifica norma procesal específica.
     c. (Opcional fase 2) RAG en `document_precedents` para mejorar plan.
     d. Persiste recipe en cache.
  4. Si TODO falla → fallback a `_resolve_plan()` legacy (templates
     Python siguen como red de seguridad — no se eliminan).

Output: `StructureRecipe` con sections_plan listo para block_generator.

Reusa al 100%:
  - VerificationAgent (mismo helper que stages 5, 7 y M19.22)
  - Mismo OpenAI client del pipeline
  - asyncpg pool del pipeline

Costo extra: ~$0.002 por cache miss (1 LLM call gpt-4o-mini).
Cache hit: 0 LLM calls, ~5ms (1 SELECT en BD).

Filosofía: descubrimiento dinámico estilo Claude — el conocimiento
está en el modelo, no en archivos Python pre-armados. Templates
hardcoded quedan como fallback de seguridad.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ============================================================
# Schema
# ============================================================

@dataclass
class StructureRecipe:
    """Plan de estructura descubierto para un documento legal."""
    structure_key: str
    doc_type: str
    jurisdiccion: Optional[str] = None
    cuantia_rango: Optional[str] = None
    demandado_tipo: Optional[str] = None
    procedimiento: Optional[str] = None

    # El plan dinámico (reemplaza SECTIONS_PLAN_BY_TYPE)
    sections_plan: list[dict] = field(default_factory=list)

    # Metadata legal
    norma_procesal_ref: Optional[str] = None
    juramento_norma_ref: Optional[str] = None
    juez_competente: Optional[str] = None
    cuerpos_normativos_minimos: list[str] = field(default_factory=list)

    # Trazabilidad
    fuentes_consultadas: list[str] = field(default_factory=list)
    generated_by: str = "gpt-4o-mini"
    generation_reasoning: str = ""

    # Flags
    cached: bool = False
    fallback_used: bool = False        # true si tuvo que usar _resolve_plan legacy
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "structure_key": self.structure_key,
            "doc_type": self.doc_type,
            "jurisdiccion": self.jurisdiccion,
            "cuantia_rango": self.cuantia_rango,
            "demandado_tipo": self.demandado_tipo,
            "procedimiento": self.procedimiento,
            "sections_plan": self.sections_plan,
            "norma_procesal_ref": self.norma_procesal_ref,
            "juramento_norma_ref": self.juramento_norma_ref,
            "juez_competente": self.juez_competente,
            "cuerpos_normativos_minimos": self.cuerpos_normativos_minimos,
            "fuentes_consultadas": self.fuentes_consultadas,
            "generated_by": self.generated_by,
            "cached": self.cached,
            "fallback_used": self.fallback_used,
            "duration_ms": self.duration_ms,
            "sections_count": len(self.sections_plan),
        }


# ============================================================
# Structure key construction
# ============================================================

def _build_structure_key(
    doc_type: str,
    jurisdiccion: Optional[str] = None,
    cuantia_rango: Optional[str] = None,
    demandado_tipo: Optional[str] = None,
) -> str:
    """Construye structure_key normalizada y determinística.

    Formato: "doc_type:jurisdiccion:cuantia_rango:demandado_tipo"
    Componentes faltantes → "_" (placeholder).
    """
    def _norm(v: Optional[str]) -> str:
        if not v:
            return "_"
        return v.strip().lower().replace(" ", "_")

    return ":".join([
        _norm(doc_type),
        _norm(jurisdiccion),
        _norm(cuantia_rango),
        _norm(demandado_tipo),
    ])


def _infer_dimensions_from_context(
    doc_type: str,
    enriched_context: Any | None = None,
    intent: str = "",
    brief: str = "",
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Infiere (jurisdiccion, cuantia_rango, demandado_tipo) del contexto.

    Si enriched_context tiene suggested_jurisdiccion, lo usa.
    Para cuantia_rango y demandado_tipo, usa heurísticas simples basadas
    en keywords del intent + brief. El LLM en _generate_plan_with_llm
    afinará estos valores.
    """
    jurisdiccion = None
    if enriched_context is not None:
        jurisdiccion = getattr(enriched_context, "suggested_jurisdiccion", None)

    # Inferir jurisdicción del doc_type si no vino del enriched
    if not jurisdiccion:
        if doc_type.startswith("demanda_civil") or doc_type in ("demanda_pertenencia", "demanda_responsabilidad_civil", "demanda_ejecutivo_singular"):
            jurisdiccion = "civil"
        elif doc_type in ("demanda_divorcio", "demanda_alimentos", "demanda_custodia"):
            jurisdiccion = "familia"
        elif doc_type in ("demanda_laboral_ordinaria", "demanda_laboral", "demanda_ejecutiva_laboral"):
            jurisdiccion = "laboral"
        elif doc_type in ("demanda_nulidad_restablecimiento", "demanda_simple_nulidad", "demanda_reparacion_directa"):
            jurisdiccion = "admin"
        elif doc_type in ("tutela", "accion_tutela"):
            jurisdiccion = "constitucional"
        elif doc_type in ("denuncia_penal",):
            jurisdiccion = "penal"

    # cuantia_rango — heurística simple
    full_text = (intent + " " + (brief or "")).lower()
    cuantia_rango: Optional[str] = None
    if any(k in full_text for k in ["mayor cuantía", "mayor cuantia", "100 smmlv", "200 smmlv", "más de 150 smmlv"]):
        cuantia_rango = "mayor"
    elif any(k in full_text for k in ["menor cuantía", "menor cuantia"]):
        cuantia_rango = "menor"
    elif any(k in full_text for k in ["mínima cuantía", "minima cuantia"]):
        cuantia_rango = "minima"
    elif jurisdiccion in ("constitucional",):
        cuantia_rango = "sin_cuantia"

    # demandado_tipo — heurística simple
    demandado_tipo: Optional[str] = None
    if any(k in full_text for k in ["dian", "sic ", "superintendencia", "ministerio", "alcaldía", "alcaldia",
                                     "gobernación", "gobernacion", "entidad pública", "entidad publica",
                                     "nación", "nacion -", "tribunal administrativo"]):
        demandado_tipo = "entidad_publica"
    elif any(k in full_text for k in [" s.a.s", " s.a ", " sas ", " ltda", "sociedad ", "nit "]):
        demandado_tipo = "persona_juridica"
    elif jurisdiccion in ("familia",) or any(k in full_text for k in ["mayor de edad", "c.c. ", "cc ", "cédula"]):
        demandado_tipo = "persona_natural"

    return jurisdiccion, cuantia_rango, demandado_tipo


# ============================================================
# Cache read/write
# ============================================================

async def _cache_get(pool, structure_key: str) -> Optional[StructureRecipe]:
    """Lee recipe del cache. None si no existe o falla."""
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT doc_type, jurisdiccion, cuantia_rango, demandado_tipo, procedimiento,
                       sections_plan, norma_procesal_ref, juramento_norma_ref,
                       juez_competente, cuerpos_normativos_minimos,
                       fuentes_consultadas, generated_by, generation_reasoning
                FROM structure_recipes
                WHERE structure_key = $1
                """,
                structure_key,
            )
            if row is None:
                return None
            # bump usage_count + last_used_at (best effort)
            try:
                await conn.execute(
                    "UPDATE structure_recipes SET usage_count = usage_count + 1, last_used_at = now() WHERE structure_key = $1",
                    structure_key,
                )
            except Exception:
                pass
        sp = row["sections_plan"] if isinstance(row["sections_plan"], list) else json.loads(row["sections_plan"] or "[]")
        cn = row["cuerpos_normativos_minimos"] if isinstance(row["cuerpos_normativos_minimos"], list) else json.loads(row["cuerpos_normativos_minimos"] or "[]")
        fc = row["fuentes_consultadas"] if isinstance(row["fuentes_consultadas"], list) else json.loads(row["fuentes_consultadas"] or "[]")
        return StructureRecipe(
            structure_key=structure_key,
            doc_type=row["doc_type"],
            jurisdiccion=row["jurisdiccion"],
            cuantia_rango=row["cuantia_rango"],
            demandado_tipo=row["demandado_tipo"],
            procedimiento=row["procedimiento"],
            sections_plan=sp,
            norma_procesal_ref=row["norma_procesal_ref"],
            juramento_norma_ref=row["juramento_norma_ref"],
            juez_competente=row["juez_competente"],
            cuerpos_normativos_minimos=cn,
            fuentes_consultadas=fc,
            generated_by=row["generated_by"] or "cache",
            generation_reasoning=row["generation_reasoning"] or "",
            cached=True,
        )
    except Exception as e:
        logger.debug("structure_recipes cache read failed: %s", e)
        return None


async def _cache_write(pool, recipe: StructureRecipe) -> None:
    """Persiste recipe en cache. Idempotente (UPSERT)."""
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO structure_recipes (
                    structure_key, doc_type, jurisdiccion, cuantia_rango,
                    demandado_tipo, procedimiento, sections_plan,
                    norma_procesal_ref, juramento_norma_ref, juez_competente,
                    cuerpos_normativos_minimos, fuentes_consultadas,
                    generated_by, generation_reasoning, usage_count, last_used_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7::jsonb,
                    $8, $9, $10, $11::jsonb, $12::jsonb, $13, $14, 1, now()
                )
                ON CONFLICT (structure_key) DO UPDATE SET
                    sections_plan = EXCLUDED.sections_plan,
                    norma_procesal_ref = EXCLUDED.norma_procesal_ref,
                    juramento_norma_ref = EXCLUDED.juramento_norma_ref,
                    juez_competente = EXCLUDED.juez_competente,
                    cuerpos_normativos_minimos = EXCLUDED.cuerpos_normativos_minimos,
                    generation_reasoning = EXCLUDED.generation_reasoning,
                    updated_at = now()
                """,
                recipe.structure_key, recipe.doc_type, recipe.jurisdiccion,
                recipe.cuantia_rango, recipe.demandado_tipo, recipe.procedimiento,
                json.dumps(recipe.sections_plan, ensure_ascii=False, default=str),
                recipe.norma_procesal_ref, recipe.juramento_norma_ref, recipe.juez_competente,
                json.dumps(recipe.cuerpos_normativos_minimos, ensure_ascii=False),
                json.dumps(recipe.fuentes_consultadas, ensure_ascii=False),
                recipe.generated_by, recipe.generation_reasoning,
            )
    except Exception as e:
        logger.warning("structure_recipes cache write failed (non-fatal): %s", e)


# ============================================================
# LLM-based plan generation
# ============================================================

STRUCTURE_DISCOVERY_PROMPT = """Eres un ABOGADO LITIGANTE SENIOR colombiano con conocimiento profundo del:
- Código General del Proceso (Ley 1564/2012) — Arts. 82-83 requisitos demanda
- Código Procesal Administrativo y de lo Contencioso Administrativo (Ley 1437/2011, CPACA)
- Código de Procedimiento Penal (Ley 906/2004)
- Código Procesal del Trabajo y de la Seguridad Social (CPTSS, Decreto-Ley 2158/1948)
- Constitución Política de 1991 (Art. 86 tutela, Art. 23 derecho de petición)
- Decreto 2591/1991 (reglamento tutela)
- Ley 472/1998 (acciones populares y de grupo)

Tu tarea es decidir la ESTRUCTURA (lista ordenada de secciones) que debe tener
un documento legal colombiano específico, basándote en:
  • doc_type
  • jurisdicción (civil, familia, laboral, admin, penal, constitucional)
  • cuantia_rango (mayor, menor, mínima, sin_cuantia)
  • demandado_tipo (persona_natural, persona_juridica, entidad_publica)

REGLAS CRÍTICAS:

1. **Estructura específica por área:**
   - Civil/Familia ordinario: encabezado, partes, hechos, pretensiones, fundamentos,
     competencia_cuantia, pruebas, anexos, notificaciones, juramento, firma.
   - Laboral: encabezado, partes, hechos, pretensiones, fundamentos,
     competencia_cuantia, liquidacion, pruebas, anexos, notificaciones, juramento, firma.
   - Admin (nulidad/restablecimiento): encabezado, partes, acto_administrativo,
     hechos, pretensiones, fundamentos, competencia, pruebas, anexos,
     notificaciones, juramento, firma.
   - Tutela: encabezado, partes, derechos_vulnerados, hechos, pretensiones,
     fundamentos, pruebas, notificaciones, firma (SIN juramento separado —
     Art. 86 CN no lo requiere).
   - Reparación directa admin: encabezado, partes, hechos, pretensiones,
     daño, fundamentos, competencia, pruebas, anexos, notificaciones,
     juramento, firma.
   - Acción popular/grupo: encabezado, partes, derechos_colectivos_vulnerados,
     hechos, pretensiones, fundamentos, pruebas, notificaciones, juramento, firma.

2. **Juramento por jurisdicción (Art. específico):**
   - Civil/Familia → "Art. 206 CGP (Ley 1564/2012)"
   - Laboral → "Art. 28 CPTSS"
   - Admin → "Art. 167 CPACA (Ley 1437/2011)"
   - Penal → "Art. 269 CPP (Ley 906/2004)"
   - Tutela → "" (no aplica)
   - Popular/Grupo → "Art. 18 Ley 472/1998"

3. **Juez competente por área:**
   - Civil mayor cuantía → "Juez Civil del Circuito"
   - Civil menor cuantía → "Juez Civil Municipal"
   - Familia → "Juez de Familia del Circuito"
   - Laboral → "Juez Laboral del Circuito"
   - Admin → "Tribunal Administrativo" o "Juez Administrativo" según cuantía
   - Tutela → "Cualquier juez (reparto)"
   - Penal → "Juez Penal Municipal/Circuito"

4. **Norma procesal aplicable:**
   - Civil/Familia → identifica el Art. específico del CGP (e.g., divorcio Art. 388-389,
     pertenencia Art. 375, alimentos Art. 397-399)
   - Admin → Art. 162-163 CPACA + Art. 138 (nulidad) o 140 (reparación)
   - Penal → Art. 67 CPP (querella) o Art. 66 (denuncia)
   - Tutela → Decreto 2591/1991

5. **Cuerpos normativos mínimos esperados:**
   - Civil → ["CGP", "CC"]
   - Familia → ["CGP", "CC", "Ley 1098/2006"]
   - Laboral → ["CST", "CPTSS"]
   - Admin → ["CPACA", "CN"]
   - Tutela → ["CN", "Decreto 2591/1991"]

OUTPUT (JSON estricto, sin markdown):
{
  "sections_plan": [
    {"key": "encabezado", "title": "ENCABEZADO", "order": 1, "roman": null,
     "expected_blocks": ["paragraph"]},
    {"key": "partes", "title": "PARTES", "order": 2, "roman": "I",
     "expected_blocks": ["section_heading", "subsection", "paragraph"]},
    ...
  ],
  "norma_procesal_ref": "Art. 388-389 CGP (Ley 1564/2012)",
  "juramento_norma_ref": "Art. 206 CGP (Ley 1564/2012)",
  "juez_competente": "Juez de Familia del Circuito",
  "cuerpos_normativos_minimos": ["CGP", "CC", "Ley 1098/2006"],
  "procedimiento": "verbal",
  "reasoning": "Una frase explicando por qué esta estructura"
}

NO inventes secciones que no son típicas. NO omitas secciones obligatorias
del área. Mantén el orden tradicional forense colombiano.
"""


async def _generate_plan_with_llm(
    client,
    doc_type: str,
    jurisdiccion: Optional[str],
    cuantia_rango: Optional[str],
    demandado_tipo: Optional[str],
    intent: str,
    enriched_context: Any | None,
) -> Optional[dict]:
    """Llama al LLM para generar el plan. None si falla."""
    if client is None:
        return None
    try:
        # Construir contexto enriquecido como guía adicional
        enriched_block = ""
        if enriched_context is not None and not getattr(enriched_context, "enrichment_skipped", True):
            sugg = getattr(enriched_context, "suggested_doc_type", "")
            conf = getattr(enriched_context, "suggested_doc_type_confidence", 0.0)
            if sugg:
                enriched_block = f"\nContexto enriquecido previo: doc_type sugerido={sugg} (confianza {conf:.2f})\n"

        user_prompt = f"""Decide la estructura del siguiente documento legal:

  doc_type:        {doc_type}
  jurisdiccion:    {jurisdiccion or '(inferir del doc_type)'}
  cuantia_rango:   {cuantia_rango or '(inferir del intent)'}
  demandado_tipo:  {demandado_tipo or '(inferir del intent)'}

Intent del usuario (primeras 800 chars):
\"\"\"
{intent[:800]}
\"\"\"
{enriched_block}
Devuelve el plan en JSON estricto según el schema del system prompt."""

        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": STRUCTURE_DISCOVERY_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=1500,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        # Validar shape mínimo
        sp = data.get("sections_plan")
        if not isinstance(sp, list) or len(sp) < 3:
            logger.warning(
                "structure_discovery: LLM returned invalid sections_plan (%s items)",
                len(sp) if isinstance(sp, list) else "non-list",
            )
            return None
        # Sanitizar cada sección
        clean_sections: list[dict] = []
        for i, s in enumerate(sp[:20]):
            if not isinstance(s, dict):
                continue
            clean_sections.append({
                "key": str(s.get("key", f"sec_{i+1}"))[:60].lower().replace(" ", "_"),
                "title": str(s.get("title", ""))[:200],
                "order": int(s.get("order", i + 1)),
                "roman": s.get("roman"),
                "expected_blocks": s.get("expected_blocks") or [],
            })
        data["sections_plan"] = clean_sections
        return data
    except Exception as e:
        logger.warning("structure_discovery LLM call failed: %s", e)
        return None


# ============================================================
# Fallback to legacy _resolve_plan
# ============================================================

def _fallback_to_legacy(doc_type: str) -> StructureRecipe:
    """Fallback de seguridad: usa _resolve_plan() legacy (templates Python).

    Esto se ejecuta solo si:
    - El LLM falla 3 veces consecutivas, O
    - El pool de BD no está disponible, O
    - Excepción no manejada en el flujo principal.
    """
    sections_plan: list[dict] = []
    try:
        # Import dinámico para evitar dependencia circular
        from lex.orchestrator.orchestrator import _resolve_plan as _legacy_resolve_plan
        legacy_plan = _legacy_resolve_plan(doc_type)
        if legacy_plan:
            sections_plan = [
                {
                    "key": s.get("key"),
                    "title": s.get("title"),
                    "order": s.get("order"),
                    "roman": s.get("roman"),
                    "expected_blocks": [],
                }
                for s in legacy_plan
            ]
    except Exception as e:
        logger.warning("structure_discovery fallback _resolve_plan failed: %s", e)
    return StructureRecipe(
        structure_key=_build_structure_key(doc_type),
        doc_type=doc_type,
        sections_plan=sections_plan,
        generated_by="fallback_template",
        fallback_used=True,
        generation_reasoning="LLM failed; fell back to hardcoded template registry",
    )


# ============================================================
# Public entry point
# ============================================================

async def discover_structure(
    client,
    pool,
    doc_type: str,
    enriched_context: Any | None = None,
    intent: str = "",
    brief: str = "",
    timeout_seconds: float = 30.0,
) -> StructureRecipe:
    """Punto de entrada principal. Invoca el orchestrator en stage 1C.

    Garantías:
    - SIEMPRE devuelve una StructureRecipe válida (nunca raise).
    - Si todo falla, fallback a templates Python legacy.
    - Timeout 30s para evitar cuelgues.
    """
    started = time.time()
    try:
        result = await asyncio.wait_for(
            _discover_inner(client, pool, doc_type, enriched_context, intent, brief),
            timeout=timeout_seconds,
        )
        result.duration_ms = int((time.time() - started) * 1000)
        return result
    except asyncio.TimeoutError:
        logger.warning("structure_discovery TIMEOUT after %.1fs — using legacy fallback", timeout_seconds)
        r = _fallback_to_legacy(doc_type)
        r.duration_ms = int((time.time() - started) * 1000)
        return r
    except Exception as e:
        logger.warning("structure_discovery exception (non-fatal): %s", e)
        r = _fallback_to_legacy(doc_type)
        r.duration_ms = int((time.time() - started) * 1000)
        return r


async def _discover_inner(
    client,
    pool,
    doc_type: str,
    enriched_context: Any | None,
    intent: str,
    brief: str,
) -> StructureRecipe:
    """Implementación interna envuelta por discover_structure con timeout."""

    # 1. Inferir dimensiones del contexto
    jurisdiccion, cuantia_rango, demandado_tipo = _infer_dimensions_from_context(
        doc_type, enriched_context, intent, brief
    )

    # 2. Construir structure_key
    structure_key = _build_structure_key(doc_type, jurisdiccion, cuantia_rango, demandado_tipo)
    logger.info("structure_discovery: key=%s", structure_key)

    # 3. Cache hit?
    cached = await _cache_get(pool, structure_key)
    if cached is not None:
        logger.info(
            "structure_discovery: CACHE HIT for %s (%d sections)",
            structure_key, len(cached.sections_plan),
        )
        return cached

    # 4. Cache miss — generar con LLM
    logger.info("structure_discovery: cache miss, generating plan with LLM")
    llm_result = await _generate_plan_with_llm(
        client, doc_type, jurisdiccion, cuantia_rango, demandado_tipo,
        intent, enriched_context,
    )

    if llm_result is None:
        logger.warning("structure_discovery: LLM failed, falling back to legacy")
        return _fallback_to_legacy(doc_type)

    # 5. Construir recipe y cachear
    recipe = StructureRecipe(
        structure_key=structure_key,
        doc_type=doc_type,
        jurisdiccion=jurisdiccion,
        cuantia_rango=cuantia_rango,
        demandado_tipo=demandado_tipo,
        procedimiento=llm_result.get("procedimiento"),
        sections_plan=llm_result.get("sections_plan", []),
        norma_procesal_ref=llm_result.get("norma_procesal_ref"),
        juramento_norma_ref=llm_result.get("juramento_norma_ref"),
        juez_competente=llm_result.get("juez_competente"),
        cuerpos_normativos_minimos=llm_result.get("cuerpos_normativos_minimos", []),
        generated_by="gpt-4o-mini",
        generation_reasoning=str(llm_result.get("reasoning", ""))[:500],
    )

    await _cache_write(pool, recipe)
    logger.info(
        "structure_discovery: GENERATED %s (%d sections, juez=%s, juramento=%s)",
        structure_key, len(recipe.sections_plan),
        recipe.juez_competente, recipe.juramento_norma_ref,
    )
    return recipe
