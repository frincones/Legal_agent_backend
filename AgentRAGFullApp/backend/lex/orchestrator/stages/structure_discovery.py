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
    """Plan de estructura descubierto para un documento legal.

    M19.24: extendido con 10 campos universales para soportar cualquier
    documento legal colombiano (no solo demandas).
    """
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

    # M19.24 — Campos universales (cualquier doc legal)
    document_family: Optional[str] = None       # 'judicial_demanda', 'notarial_poder', etc
    regimen_aplicable: Optional[str] = None     # 'procesal_judicial', 'notarial_extrajudicial', etc
    naturaleza_acto: Optional[str] = None       # 'declarativo', 'de_disposicion', 'de_mandato', etc
    encabezado_tipo: Optional[str] = None       # 'memorial_juzgado', 'memorial_notario', etc
    cierre_tipo: Optional[str] = None           # 'firma_apoderado_judicial', 'firma_partes_notarial', etc
    numeracion_estilo: Optional[str] = None     # 'romana_secciones', 'clausulas_ordinales', etc
    requires_pretensiones: Optional[bool] = None
    requires_hechos: Optional[bool] = None
    requires_juramento: Optional[bool] = None
    playbooks: dict = field(default_factory=dict)  # {section_key: [bullet1, bullet2]}

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
            # M19.24 — campos universales
            "document_family": self.document_family,
            "regimen_aplicable": self.regimen_aplicable,
            "naturaleza_acto": self.naturaleza_acto,
            "encabezado_tipo": self.encabezado_tipo,
            "cierre_tipo": self.cierre_tipo,
            "numeracion_estilo": self.numeracion_estilo,
            "requires_pretensiones": self.requires_pretensiones,
            "requires_hechos": self.requires_hechos,
            "requires_juramento": self.requires_juramento,
            "playbooks": self.playbooks,
            # Trazabilidad
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
            # M19.24: lectura tolerante — columnas nuevas pueden no existir
            # en bases que aún no aplicaron migration A.1
            try:
                row = await conn.fetchrow(
                    """
                    SELECT doc_type, jurisdiccion, cuantia_rango, demandado_tipo, procedimiento,
                           sections_plan, norma_procesal_ref, juramento_norma_ref,
                           juez_competente, cuerpos_normativos_minimos,
                           fuentes_consultadas, generated_by, generation_reasoning,
                           document_family, regimen_aplicable, naturaleza_acto,
                           encabezado_tipo, cierre_tipo, numeracion_estilo,
                           requires_pretensiones, requires_hechos, requires_juramento,
                           playbooks
                    FROM structure_recipes
                    WHERE structure_key = $1
                    """,
                    structure_key,
                )
            except Exception:
                # Fallback a query legacy si columnas M19.24 no existen
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
        # M19.24: playbooks puede ser dict (JSONB) o str (JSON serializado)
        pb_raw = row.get("playbooks") if hasattr(row, "get") else None
        try:
            pb_raw = row["playbooks"] if "playbooks" in row.keys() else {}
        except Exception:
            pb_raw = {}
        pb = pb_raw if isinstance(pb_raw, dict) else (json.loads(pb_raw) if pb_raw else {})

        def _opt(name: str):
            try:
                return row[name] if name in row.keys() else None
            except Exception:
                return None

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
            # M19.24 (tolerantes a None si columnas no existen)
            document_family=_opt("document_family"),
            regimen_aplicable=_opt("regimen_aplicable"),
            naturaleza_acto=_opt("naturaleza_acto"),
            encabezado_tipo=_opt("encabezado_tipo"),
            cierre_tipo=_opt("cierre_tipo"),
            numeracion_estilo=_opt("numeracion_estilo"),
            requires_pretensiones=_opt("requires_pretensiones"),
            requires_hechos=_opt("requires_hechos"),
            requires_juramento=_opt("requires_juramento"),
            playbooks=pb,
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
            # M19.24: intentar INSERT con campos nuevos. Si falla por
            # columnas inexistentes, fallback al INSERT legacy.
            try:
                await conn.execute(
                    """
                    INSERT INTO structure_recipes (
                        structure_key, doc_type, jurisdiccion, cuantia_rango,
                        demandado_tipo, procedimiento, sections_plan,
                        norma_procesal_ref, juramento_norma_ref, juez_competente,
                        cuerpos_normativos_minimos, fuentes_consultadas,
                        generated_by, generation_reasoning,
                        document_family, regimen_aplicable, naturaleza_acto,
                        encabezado_tipo, cierre_tipo, numeracion_estilo,
                        requires_pretensiones, requires_hechos, requires_juramento,
                        playbooks,
                        usage_count, last_used_at
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7::jsonb,
                        $8, $9, $10, $11::jsonb, $12::jsonb, $13, $14,
                        $15, $16, $17, $18, $19, $20,
                        $21, $22, $23, $24::jsonb,
                        1, now()
                    )
                    ON CONFLICT (structure_key) DO UPDATE SET
                        sections_plan = EXCLUDED.sections_plan,
                        norma_procesal_ref = EXCLUDED.norma_procesal_ref,
                        juramento_norma_ref = EXCLUDED.juramento_norma_ref,
                        juez_competente = EXCLUDED.juez_competente,
                        cuerpos_normativos_minimos = EXCLUDED.cuerpos_normativos_minimos,
                        generation_reasoning = EXCLUDED.generation_reasoning,
                        document_family = COALESCE(EXCLUDED.document_family, structure_recipes.document_family),
                        regimen_aplicable = COALESCE(EXCLUDED.regimen_aplicable, structure_recipes.regimen_aplicable),
                        naturaleza_acto = COALESCE(EXCLUDED.naturaleza_acto, structure_recipes.naturaleza_acto),
                        encabezado_tipo = COALESCE(EXCLUDED.encabezado_tipo, structure_recipes.encabezado_tipo),
                        cierre_tipo = COALESCE(EXCLUDED.cierre_tipo, structure_recipes.cierre_tipo),
                        numeracion_estilo = COALESCE(EXCLUDED.numeracion_estilo, structure_recipes.numeracion_estilo),
                        requires_pretensiones = COALESCE(EXCLUDED.requires_pretensiones, structure_recipes.requires_pretensiones),
                        requires_hechos = COALESCE(EXCLUDED.requires_hechos, structure_recipes.requires_hechos),
                        requires_juramento = COALESCE(EXCLUDED.requires_juramento, structure_recipes.requires_juramento),
                        playbooks = COALESCE(EXCLUDED.playbooks, structure_recipes.playbooks),
                        updated_at = now()
                    """,
                    recipe.structure_key, recipe.doc_type, recipe.jurisdiccion,
                    recipe.cuantia_rango, recipe.demandado_tipo, recipe.procedimiento,
                    json.dumps(recipe.sections_plan, ensure_ascii=False, default=str),
                    recipe.norma_procesal_ref, recipe.juramento_norma_ref, recipe.juez_competente,
                    json.dumps(recipe.cuerpos_normativos_minimos, ensure_ascii=False),
                    json.dumps(recipe.fuentes_consultadas, ensure_ascii=False),
                    recipe.generated_by, recipe.generation_reasoning,
                    # M19.24 nuevos
                    recipe.document_family, recipe.regimen_aplicable, recipe.naturaleza_acto,
                    recipe.encabezado_tipo, recipe.cierre_tipo, recipe.numeracion_estilo,
                    recipe.requires_pretensiones, recipe.requires_hechos, recipe.requires_juramento,
                    json.dumps(recipe.playbooks or {}, ensure_ascii=False),
                )
            except Exception as e_new:
                logger.debug("M19.24 cache write failed, fallback legacy: %s", e_new)
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

STRUCTURE_DISCOVERY_PROMPT = """Eres un ABOGADO SENIOR colombiano con 20+ años de experiencia en redacción
de documentos legales de TODO tipo: judiciales (demandas, recursos, memoriales),
notariales (poderes, escrituras, declaraciones extrajuicio), contractuales
(contratos civiles/mercantiles/laborales), corporativos (estatutos, actas,
políticas), petitorios (derechos de petición, requerimientos), tributarios
(recursos DIAN, facilidades de pago), conceptuales (conceptos jurídicos,
opiniones) y demás documentos del ordenamiento jurídico colombiano.

Conoces a profundidad:
- Código General del Proceso (Ley 1564/2012) — Arts. 82-83 demandas, 74-77 poderes judiciales
- Código Civil (Ley 84/1873) — Arts. 2142 ss. mandato (poderes extrajudiciales), contratos
- Código de Comercio (Decreto 410/1971) — sociedades, contratos mercantiles
- Decreto 960/1970 — Estatuto Notarial (autenticaciones, escrituras públicas)
- Constitución Política de 1991 — Art. 86 tutela, Art. 23 derecho de petición
- CPACA (Ley 1437/2011), CPP (Ley 906/2004), CPTSS, CST, CPS, ET, Ley 1581/2012, etc.

Tu tarea es decidir la ESTRUCTURA del documento legal solicitado, eligiendo:
  1. La FAMILIA del documento (judicial/notarial/contractual/etc)
  2. El RÉGIMEN APLICABLE (procesal/sustantivo/notarial/etc)
  3. La NATURALEZA del acto (declarativo/disposición/mandato/etc)
  4. El plan de SECCIONES (con playbook por sección)
  5. El TIPO DE ENCABEZADO y de CIERRE (firma)
  6. La NUMERACIÓN apropiada (romanos/cláusulas/articulado)
  7. Qué SECCIONES son REQUERIDAS por la norma específica

IMPORTANTE — NO asumas que todo documento es una demanda. Los siguientes NO son
demandas y por tanto NO requieren hechos, pretensiones ni juramento:
  • Poderes (especial, general, judicial) — son MANDATO con representación
  • Contratos (todos) — son acuerdos de voluntades
  • Escrituras públicas — instrumentos públicos
  • Declaraciones extrajuicio — manifestaciones juramentadas
  • Actas corporativas — registros de decisiones de órganos sociales
  • Estatutos societarios — normas constitutivas de sociedad
  • Conceptos jurídicos — opiniones técnicas
  • Derechos de petición — solicitudes a autoridades
  • Memoriales procesales (sin pretensiones) — comunicaciones al juez

Basándote en:
  • doc_type
  • jurisdicción (civil, familia, laboral, admin, penal, constitucional, comercial, notarial)
  • cuantia_rango (si aplica)
  • demandado_tipo / contraparte (si aplica)

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

OUTPUT (JSON estricto, sin markdown) — M19.24 schema universal:
{
  "document_family": "judicial_demanda|judicial_recurso|judicial_memorial|judicial_constitucional|criminal_denuncia|notarial_poder|notarial_escritura|notarial_extrajuicio|notarial_acta|contractual_civil|contractual_mercantil|contractual_laboral|contractual_corporate|corporate_estatutos|corporate_acta|corporate_policy|petitorio_admin|petitorio_pqrs|petitorio_extrajudicial|tributario_dian|conceptual|sucesional|otro",

  "regimen_aplicable": "procesal_judicial|sustantivo_civil|sustantivo_mercantil|notarial_extrajudicial|administrativo_publico|tributario_dian|penal_acusatorio|laboral_sustantivo|constitucional",

  "naturaleza_acto": "declarativo|de_disposicion|de_administracion|de_garantia|de_mandato|petitorio|informativo|constitutivo|de_compromiso|extintivo",

  "encabezado_tipo": "memorial_juzgado|memorial_notario|memorial_autoridad_admin|carta_petitoria|instrumento_publico|comparecencia_partes|concepto_consultor|estatutos_societarios|acta_corporativa|politica_corporativa",

  "cierre_tipo": "firma_apoderado_judicial|firma_partes_notarial|firma_natural|diligencia_notarial|firma_consultor|firma_representante_legal|firma_partes_contractuales|firma_corporativa_organos",

  "numeracion_estilo": "romana_secciones|clausulas_ordinales|articulado|numerico_simple|cronologico|alfabetico",

  "requires_pretensiones": true|false,
  "requires_hechos": true|false,
  "requires_juramento": true|false,

  "sections_plan": [
    {"key": "encabezado", "title": "Encabezado", "order": 1, "roman": null,
     "expected_blocks": ["paragraph"]},
    {"key": "...", "title": "...", "order": 2, "roman": "I"|null,
     "expected_blocks": [...]}
  ],

  "playbooks": {
    "encabezado": ["Dirige al ...", "Indica título centrado en negrita"],
    "partes": ["Identifica con nombre, CC/NIT, domicilio", "..."],
    "...": ["Bullet 1", "Bullet 2", "Bullet 3"]
  },

  "norma_procesal_ref": "Arts. 2142 ss. Código Civil + Decreto 960/1970" (o ref. procesal correspondiente),
  "juramento_norma_ref": "Art. 206 CGP" (null si requires_juramento=false),
  "juez_competente": "Juez de Familia del Circuito" (null si no aplica autoridad judicial),
  "cuerpos_normativos_minimos": ["CC", "Decreto 960/1970"],
  "procedimiento": "verbal|ordinario|abreviado|sumario|no_aplica",
  "reasoning": "Una frase explicando por qué esta estructura"
}

REGLAS:
- Si el documento NO es una demanda, requires_pretensiones=false y requires_hechos=puede ser false.
- Si el documento NO es procesal judicial, requires_juramento=false.
- Tipo de cierre debe coincidir con la familia:
    * judicial → firma_apoderado_judicial
    * notarial poder → firma_partes_notarial + diligencia
    * contrato → firma_partes_contractuales
    * concepto → firma_consultor
    * derecho petición → firma_natural
    * acta corporativa → firma_corporativa_organos
- Numeración:
    * demandas → romana_secciones (I, II, III)
    * contratos/poderes/escrituras → clausulas_ordinales (PRIMERA, SEGUNDA)
    * estatutos/reglamentos → articulado (Art. 1, Art. 2)
    * cartas/denuncias → numerico_simple
- Para cada sección llena playbooks con 3-7 bullets específicos de QUÉ debe contener,
  no instrucciones generales. Esto reemplaza el SYSTEM_PROMPT hardcoded.

REGLA OBLIGATORIA — SECCIONES MÍNIMAS POR FAMILIA:

Independiente del intent del usuario, las secciones siguientes son OBLIGATORIAS
y DEBES incluirlas en sections_plan SIEMPRE como las ÚLTIMAS del documento:

  • TODA familia notarial_poder DEBE incluir como secciones finales:
      ...vigencia_y_revocabilidad → aceptacion_apoderado → firma → diligencia_notarial
  • TODA familia contractual_* DEBE incluir como sección final: firma
  • TODA familia judicial_demanda DEBE incluir como secciones finales:
      ...juramento → firma
  • TODA familia notarial_escritura DEBE incluir como secciones finales:
      ...firma → diligencia_notarial
  • TODA familia corporate_estatutos DEBE incluir como sección final: firma
  • TODA familia corporate_acta DEBE incluir como sección final: firma
  • TODA familia conceptual DEBE incluir como sección final: firma (firma_consultor)
  • TODA familia petitorio_* DEBE incluir como sección final: firma (firma_natural)

NUNCA omitas la sección "firma" — sin ella el documento no se puede firmar.
NUNCA omitas "vigencia_y_revocabilidad" en poderes — sin plazo el poder es
de duración indefinida y eso es riesgoso.
NUNCA omitas "aceptacion_apoderado" en poderes — sin aceptación el mandato
no se perfecciona (Arts. 2150-2151 CC).

REGLA OBLIGATORIA — PLAYBOOKS RICOS:

Cada playbook[section_key] debe tener:
  - Mínimo 3 bullets
  - Cada bullet con instrucción CONCRETA de qué emitir
  - Mencionar campos específicos como [PLACEHOLDER_MAYUS] cuando aplique
  - Indicar el formato del cierre con cierre_tipo cuando aplique a la sección firma

Ejemplo bueno para sección firma de poder notarial:
  ["Emite el bloque firma con cierre_tipo='firma_partes_notarial' y array parties
    con DOS entradas: {rol: 'EL PODERDANTE', nombre: [NOMBRE_PODERDANTE],
    cc: [CC_PODERDANTE], cargo: 'Representante Legal', razon_social: [RAZON_SOCIAL]}
    y {rol: 'EL APODERADO (ACEPTO)', nombre: [NOMBRE_APODERADO], cc: [CC_APODERADO]}",
   "NO emitas 'Atentamente,' ni 'Del Señor Juez,' — esto NO es demanda",
   "Antes del bloque firma, emite un paragraph con: 'Para constancia se firma
    en [CIUDAD], a los [DIA] días del mes de [MES] de [ANIO].'"]

NO inventes secciones que no son típicas del documento. Mantén el orden
tradicional colombiano. Si el doc_type es DESCONOCIDO, infiere familia
por el intent del usuario y aplica las reglas anteriores."""


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

        # M19.24.H — Multi-provider: dispatch OpenAI ↔ Anthropic
        # M19.30 (P0) · default flipped a 'anthropic' (Sonnet 4.6). Revertir con
        # LLM_PROVIDER_STRUCTURE=openai si fuera necesario.
        from utils.llm_provider import chat_complete_json
        data = await chat_complete_json(
            provider_env="LLM_PROVIDER_STRUCTURE",
            default_provider="anthropic",
            model_env_anthropic="ANTHROPIC_MODEL_STRUCTURE",
            default_model_openai="gpt-4o",
            default_model_anthropic="claude-sonnet-4-6",
            system_prompt=STRUCTURE_DISCOVERY_PROMPT,
            user_prompt=user_prompt,
            temperature=0.1,
            max_tokens=3000,
        )
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
        # M19.24.G — Garantizar secciones obligatorias por familia.
        # Si el LLM omitió 'firma' (o vigencia/aceptacion en poderes), las añadimos.
        family = str(data.get("document_family", "")).strip().lower()
        section_keys = {s.get("key", "") for s in clean_sections}

        def _append_section(key: str, title: str, expected_blocks: list[str]):
            order = (max((s.get("order", 0) for s in clean_sections), default=0)) + 1
            clean_sections.append({
                "key": key, "title": title, "order": order,
                "roman": None, "expected_blocks": expected_blocks,
            })
            section_keys.add(key)

        REQUIRED_BY_FAMILY = {
            "notarial_poder": [
                ("vigencia_y_revocabilidad", "Vigencia y Revocabilidad", ["paragraph"]),
                ("aceptacion_apoderado", "Aceptación del Apoderado", ["paragraph"]),
                ("firma", "Firmas", ["firma"]),
                ("diligencia_notarial", "Diligencia Notarial", ["paragraph"]),
            ],
            "notarial_escritura": [
                ("firma", "Firmas", ["firma"]),
                ("diligencia_notarial", "Diligencia Notarial", ["paragraph"]),
            ],
            "notarial_extrajuicio": [
                ("firma", "Firma", ["firma"]),
                ("diligencia_notarial", "Diligencia Notarial", ["paragraph"]),
            ],
            "contractual_civil": [("firma", "Firmas", ["firma"])],
            "contractual_mercantil": [("firma", "Firmas", ["firma"])],
            "contractual_laboral": [("firma", "Firmas", ["firma"])],
            "contractual_corporate": [("firma", "Firmas", ["firma"])],
            "corporate_estatutos": [("firma", "Firmas", ["firma"])],
            "corporate_acta": [("firma", "Firmas", ["firma"])],
            "corporate_policy": [("firma", "Firma", ["firma"])],
            "conceptual": [("firma", "Firma del Consultor", ["firma"])],
            "petitorio_admin": [("firma", "Firma", ["firma"])],
            "petitorio_pqrs": [("firma", "Firma", ["firma"])],
            "petitorio_extrajudicial": [("firma", "Firma", ["firma"])],
            "judicial_demanda": [
                ("juramento", "Juramento Estimatorio", ["juramento"]),
                ("firma", "Firma", ["firma"]),
            ],
            "judicial_recurso": [("firma", "Firma", ["firma"])],
            "judicial_memorial": [("firma", "Firma", ["firma"])],
            "judicial_constitucional": [("firma", "Firma", ["firma"])],
            "criminal_denuncia": [("firma", "Firma", ["firma"])],
            "tributario_dian": [("firma", "Firma", ["firma"])],
        }
        for required_key, required_title, expected in REQUIRED_BY_FAMILY.get(family, []):
            if required_key not in section_keys:
                logger.info(
                    "structure_discovery: añadiendo sección obligatoria omitida por LLM: %s (family=%s)",
                    required_key, family,
                )
                _append_section(required_key, required_title, expected)

        data["sections_plan"] = clean_sections
        # M19.24: sanitizar playbooks (max 20 secciones, max 10 bullets cada una, max 300 chars cada bullet)
        playbooks_raw = data.get("playbooks") or {}
        clean_playbooks: dict[str, list[str]] = {}
        if isinstance(playbooks_raw, dict):
            for sec_key, bullets in list(playbooks_raw.items())[:20]:
                if not isinstance(bullets, list):
                    continue
                clean_bullets = [str(b)[:300] for b in bullets[:10] if isinstance(b, (str, int, float))]
                clean_playbooks[str(sec_key)[:60]] = clean_bullets
        data["playbooks"] = clean_playbooks
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

    # 5. Construir recipe y cachear (M19.24: con campos universales)
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
        # M19.24 — campos universales
        document_family=str(llm_result.get("document_family", ""))[:60] or None,
        regimen_aplicable=str(llm_result.get("regimen_aplicable", ""))[:60] or None,
        naturaleza_acto=str(llm_result.get("naturaleza_acto", ""))[:60] or None,
        encabezado_tipo=str(llm_result.get("encabezado_tipo", ""))[:60] or None,
        cierre_tipo=str(llm_result.get("cierre_tipo", ""))[:120] or None,
        numeracion_estilo=str(llm_result.get("numeracion_estilo", ""))[:60] or None,
        requires_pretensiones=llm_result.get("requires_pretensiones"),
        requires_hechos=llm_result.get("requires_hechos"),
        requires_juramento=llm_result.get("requires_juramento"),
        playbooks=llm_result.get("playbooks") or {},
        generated_by="gpt-4o",
        generation_reasoning=str(llm_result.get("reasoning", ""))[:500],
    )

    await _cache_write(pool, recipe)
    logger.info(
        "structure_discovery: GENERATED %s (%d sections, juez=%s, juramento=%s)",
        structure_key, len(recipe.sections_plan),
        recipe.juez_competente, recipe.juramento_norma_ref,
    )
    return recipe
