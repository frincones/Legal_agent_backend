"""M19.24.B.2 — Legal Classifier Stage.

Reproduce el "Paso 3" de Claude (clasificación conceptual + corrección
de premisas legales del usuario).

Salida: LegalClassification con régimen, naturaleza, fundamento normativo,
premisas corregidas y advertencias de riesgo.

Se ejecuta ANTES del structure_discovery para que el agente:
1. Detecte si el usuario citó normas inexistentes (e.g. Art. 836 CGP)
2. Identifique el régimen correcto (procesal vs notarial vs sustantivo)
3. Sugiera correcciones de fundamento normativo
4. Levante advertencias de riesgo (Sin tope de cuantía el poder es riesgoso...)

Cache: legal_classifications_cache indexa por sha256(intent normalizado).
Costo: ~$0.003-0.005 por classification (gpt-4o, ~3-8s).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ============================================================
# Schema
# ============================================================

@dataclass
class PremisaCorregida:
    """Una corrección de premisa legal del usuario."""
    usuario_dijo: str            # "Art. 836 del Código General del Proceso"
    correcto: str                 # "Arts. 2142 y ss. del Código Civil + Decreto 960/1970"
    razon: str                    # "El CGP llega hasta el Art. 627..."
    fuente: Optional[str] = None


@dataclass
class CitaVerificada:
    """Resultado de verificar una cita del usuario contra article_index."""
    ref: str                      # cita textual
    exists: bool
    max_in_law: Optional[int] = None
    suggested_correction: Optional[str] = None


@dataclass
class LegalClassification:
    """Output del stage legal_classifier."""
    document_family: str = "otro"
    regimen_aplicable: Optional[str] = None
    naturaleza_acto: Optional[str] = None
    fundamento_normativo: list[str] = field(default_factory=list)
    premisas_corregidas: list[PremisaCorregida] = field(default_factory=list)
    advertencias_riesgo: list[str] = field(default_factory=list)
    citas_verificadas: list[CitaVerificada] = field(default_factory=list)
    reasoning: str = ""

    cached: bool = False
    skipped: bool = False
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "document_family": self.document_family,
            "regimen_aplicable": self.regimen_aplicable,
            "naturaleza_acto": self.naturaleza_acto,
            "fundamento_normativo": self.fundamento_normativo,
            "premisas_corregidas": [asdict(p) for p in self.premisas_corregidas],
            "advertencias_riesgo": self.advertencias_riesgo,
            "citas_verificadas": [asdict(c) for c in self.citas_verificadas],
            "reasoning": self.reasoning,
            "cached": self.cached,
            "skipped": self.skipped,
            "duration_ms": self.duration_ms,
        }


EMPTY_CLASSIFICATION = LegalClassification(
    document_family="otro",
    skipped=True,
    reasoning="legal_classifier skipped (no client, no pool, or disabled)",
)


# ============================================================
# Prompt
# ============================================================

LEGAL_CLASSIFIER_PROMPT = """Eres un ABOGADO SENIOR colombiano. Antes de redactar el documento legal,
tu trabajo es CLASIFICAR CONCEPTUALMENTE el caso y CORREGIR cualquier
premisa legal incorrecta del usuario.

OUTPUT (JSON estricto):
{
  "document_family": "judicial_demanda|judicial_recurso|judicial_memorial|judicial_constitucional|criminal_denuncia|notarial_poder|notarial_escritura|notarial_extrajuicio|notarial_acta|contractual_civil|contractual_mercantil|contractual_laboral|contractual_corporate|corporate_estatutos|corporate_acta|corporate_policy|petitorio_admin|petitorio_pqrs|petitorio_extrajudicial|tributario_dian|conceptual|sucesional|otro",

  "regimen_aplicable": "procesal_judicial|sustantivo_civil|sustantivo_mercantil|notarial_extrajudicial|administrativo_publico|tributario_dian|penal_acusatorio|laboral_sustantivo|constitucional",

  "naturaleza_acto": "declarativo|de_disposicion|de_administracion|de_garantia|de_mandato|petitorio|informativo|constitutivo|de_compromiso|extintivo",

  "fundamento_normativo": ["Arts. 2142 ss. CC", "Decreto 960/1970 (Estatuto Notarial)"],

  "premisas_corregidas": [
    {
      "usuario_dijo": "Art. 836 del CGP",
      "correcto": "Arts. 2142 ss. CC + Decreto 960/1970",
      "razon": "El CGP llega hasta el Art. 627. El régimen del Art. 74 CGP es para poderes JUDICIALES, no extrajudiciales. Lo que el usuario describe es un mandato con representación de naturaleza extrajudicial, regido por el CC."
    }
  ],

  "advertencias_riesgo": [
    "Sin tope máximo de cuantía el poder para comprometer patrimonio social es de alto riesgo legal",
    "Si los estatutos exigen autorización de junta directiva para garantías, el acto puede ser inoponible"
  ],

  "reasoning": "Análisis conceptual: distinguí poder judicial (Art. 74 CGP) de mandato extrajudicial (Arts. 2142 CC). El caso es del segundo tipo porque ..."
}

METODOLOGÍA OBLIGATORIA — PASO POR PASO:

PASO 1 — Lee el intent y detecta el régimen correcto:
  • Procesal judicial: si pide actuar en proceso judicial (demanda, contestación, recurso)
  • Notarial extrajudicial: si pide otorgar poder/escritura/declaración ante notaría
  • Sustantivo civil/mercantil: si pide redactar contrato
  • Administrativo público: si pide actuar ante entidad pública (derecho petición, recurso admin)
  • Tributario DIAN: si pide actuar ante DIAN o tributos
  • Penal acusatorio: si pide denunciar o querellar

PASO 2 — Identifica la NATURALEZA del acto:
  • declarativo: pide reconocimiento de derecho
  • de_disposicion: vende, dona, grava, transfiere
  • de_administracion: contrato gestión, arrendamiento
  • de_garantia: hipoteca, prenda
  • de_mandato: poder, sustitución
  • petitorio: pide algo a una autoridad
  • informativo: solo informa
  • constitutivo: crea una persona jurídica o acto nuevo
  • de_compromiso: contrato bilateral con obligaciones recíprocas
  • extintivo: revoca, desiste, renuncia

PASO 3 — VALIDA LAS CITAS del usuario contra el ordenamiento real:
  • Si el usuario cita Art. X de Ley Y, verifica que ese artículo exista
  • Códigos colombianos: CGP (Ley 1564/2012, 627 arts), CC (Ley 84/1873, 2684 arts),
    CST (Decreto 2663/1950, 491 arts), CN 1991 (380 arts), CPACA (Ley 1437/2011, 309 arts),
    CPP (Ley 906/2004, 538 arts), CP (Ley 599/2000, 478 arts), CCo (Decreto 410/1971, 2037 arts),
    Estatuto Notarial (Decreto 960/1970, 187 arts).
  • Si la cita es FALSA o el régimen no aplica → entra en premisas_corregidas.

PASO 4 — Lista el FUNDAMENTO NORMATIVO CORRECTO para este tipo de acto:
  • Poder extrajudicial → Arts. 2142 ss. CC + Decreto 960/1970
  • Poder judicial → Arts. 74-77 CGP
  • Contrato civil → arts. específicos CC según tipo
  • Demanda civil → Arts. 82-90 CGP + arts. sustantivos
  • Demanda laboral → CST + arts. 25-38 CPTSS
  • Tutela → Art. 86 CN + Decreto 2591/1991
  • Derecho petición → Art. 23 CN + Arts. 13-33 CPACA
  • Etc.

PASO 5 — LEVANTA ADVERTENCIAS DE RIESGO ESPECÍFICAS:
  • Para poderes: tope de cuantía, restricciones estatutarias, vigencia
  • Para contratos: cláusula penal, jurisdicción, terminación
  • Para demandas: caducidad, prescripción, competencia
  • Para escrituras: registro, gastos, paz y salvos
  • Para denuncias: prueba inicial, querellabilidad

NO inventes correcciones que no aplican. Si el usuario no citó normas
específicas, premisas_corregidas puede ser []. Las advertencias_riesgo
deben ser concretas y aplicables al caso, no genéricas.
"""


# ============================================================
# Cache helpers
# ============================================================

def _normalize_prompt(intent: str, doc_type: Optional[str]) -> str:
    """Normaliza el intent para hashing del cache."""
    txt = (intent or "").strip().lower()
    # Quitar espacios redundantes
    txt = re.sub(r"\s+", " ", txt)
    # Quitar puntuación menor
    txt = re.sub(r"[.,;:!?]", "", txt)
    return f"{(doc_type or '_').strip().lower()}::{txt[:2000]}"


def _hash_prompt(normalized: str) -> str:
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def _cache_get(pool, prompt_hash: str) -> Optional[LegalClassification]:
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT document_family, regimen_aplicable, naturaleza_acto,
                       fundamento_normativo, premisas_corregidas, advertencias_riesgo,
                       citas_verificadas, reasoning, generated_by, duration_ms
                FROM legal_classifications_cache
                WHERE prompt_hash = $1
                """,
                prompt_hash,
            )
            if row is None:
                return None
            try:
                await conn.execute(
                    "UPDATE legal_classifications_cache SET usage_count = usage_count + 1, last_used_at = now() WHERE prompt_hash = $1",
                    prompt_hash,
                )
            except Exception:
                pass

        def _json(field_value, default):
            if field_value is None:
                return default
            if isinstance(field_value, (list, dict)):
                return field_value
            try:
                return json.loads(field_value)
            except Exception:
                return default

        fundamento = _json(row["fundamento_normativo"], [])
        premisas_raw = _json(row["premisas_corregidas"], [])
        advertencias = _json(row["advertencias_riesgo"], [])
        citas_raw = _json(row["citas_verificadas"], [])

        return LegalClassification(
            document_family=row["document_family"],
            regimen_aplicable=row["regimen_aplicable"],
            naturaleza_acto=row["naturaleza_acto"],
            fundamento_normativo=fundamento if isinstance(fundamento, list) else [],
            premisas_corregidas=[
                PremisaCorregida(
                    usuario_dijo=p.get("usuario_dijo", ""),
                    correcto=p.get("correcto", ""),
                    razon=p.get("razon", ""),
                    fuente=p.get("fuente"),
                )
                for p in (premisas_raw if isinstance(premisas_raw, list) else [])
            ],
            advertencias_riesgo=advertencias if isinstance(advertencias, list) else [],
            citas_verificadas=[
                CitaVerificada(
                    ref=c.get("ref", ""),
                    exists=c.get("exists", True),
                    max_in_law=c.get("max_in_law"),
                    suggested_correction=c.get("suggested_correction"),
                )
                for c in (citas_raw if isinstance(citas_raw, list) else [])
            ],
            reasoning=row["reasoning"] or "",
            cached=True,
            duration_ms=row["duration_ms"] or 0,
        )
    except Exception as e:
        logger.debug("legal_classifications_cache read failed: %s", e)
        return None


async def _cache_write(pool, prompt_hash: str, intent_preview: str,
                       doc_type_hint: Optional[str],
                       classification: LegalClassification) -> None:
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO legal_classifications_cache (
                  prompt_hash, intent_preview, doc_type_hint,
                  document_family, regimen_aplicable, naturaleza_acto,
                  fundamento_normativo, premisas_corregidas, advertencias_riesgo,
                  citas_verificadas, generated_by, duration_ms, reasoning,
                  usage_count, last_used_at
                ) VALUES (
                  $1, $2, $3, $4, $5, $6,
                  $7::jsonb, $8::jsonb, $9::jsonb, $10::jsonb,
                  $11, $12, $13, 1, now()
                )
                ON CONFLICT (prompt_hash) DO UPDATE SET
                  fundamento_normativo = EXCLUDED.fundamento_normativo,
                  premisas_corregidas  = EXCLUDED.premisas_corregidas,
                  advertencias_riesgo  = EXCLUDED.advertencias_riesgo,
                  updated_at           = now()
                """,
                prompt_hash, intent_preview[:300], doc_type_hint,
                classification.document_family, classification.regimen_aplicable,
                classification.naturaleza_acto,
                json.dumps(classification.fundamento_normativo, ensure_ascii=False),
                json.dumps([asdict(p) for p in classification.premisas_corregidas], ensure_ascii=False),
                json.dumps(classification.advertencias_riesgo, ensure_ascii=False),
                json.dumps([asdict(c) for c in classification.citas_verificadas], ensure_ascii=False),
                "gpt-4o", classification.duration_ms,
                classification.reasoning[:1000],
            )
    except Exception as e:
        logger.debug("legal_classifications_cache write failed: %s", e)


# ============================================================
# LLM call + article verification
# ============================================================

async def _llm_classify(client, intent: str, doc_type_hint: Optional[str]) -> Optional[dict]:
    if client is None:
        return None
    try:
        user_msg = f"""DOC TYPE SUGERIDO POR FRONTEND: {doc_type_hint or '(ninguno)'}

INTENT DEL USUARIO:
\"\"\"
{intent[:4000]}
\"\"\"

Aplica la METODOLOGÍA OBLIGATORIA (Pasos 1-5). Devuelve JSON estricto."""

        # M19.24.H — Multi-provider
        from utils.llm_provider import chat_complete_json
        data = await chat_complete_json(
            provider_env="LLM_PROVIDER_LEGAL_CLASSIFIER",
            default_provider="openai",
            model_env_anthropic="ANTHROPIC_MODEL_LEGAL",
            default_model_openai="gpt-4o",
            default_model_anthropic="claude-sonnet-4-6",
            system_prompt=LEGAL_CLASSIFIER_PROMPT,
            user_prompt=user_msg,
            temperature=0.1,
            max_tokens=2500,
        )
        return data
    except Exception as e:
        logger.warning("legal_classifier LLM call failed: %s", e)
        return None


async def _verify_user_citations_with_article_index(pool, intent: str) -> list[CitaVerificada]:
    """Extrae citas Art. X Y del intent y las valida con article_index."""
    try:
        from lex.verify.article_index import parse_article_citation, verify_article_exists
    except Exception:
        return []

    # Encontrar TODAS las menciones tipo "Art. X Y" en el intent
    refs: list[str] = []
    seen = set()
    for m in re.finditer(
        r"art(?:[íi]culo|\.?)\s+\d{1,5}\s+[A-Za-zÁÉÍÓÚáéíóúñÑ\.\s]{2,60}",
        intent or "",
        re.IGNORECASE,
    ):
        candidate = m.group(0).strip()
        # Limit length to avoid catching too much context
        candidate = re.sub(r"\s+", " ", candidate)[:80]
        if candidate.lower() in seen:
            continue
        seen.add(candidate.lower())
        refs.append(candidate)
        if len(refs) >= 10:
            break

    if not refs:
        return []

    verdicts: list[CitaVerificada] = []
    for ref in refs:
        try:
            v = await verify_article_exists(pool, ref)
            verdicts.append(CitaVerificada(
                ref=v.cita_original,
                exists=v.exists,
                max_in_law=v.max_articulo_in_law,
                suggested_correction=v.suggested_correction,
            ))
        except Exception as e:
            logger.debug("verify_article_exists failed for %s: %s", ref, e)
    return verdicts


# ============================================================
# Public entry point
# ============================================================

async def classify_legal_case(
    client,
    pool,
    intent: str,
    doc_type_hint: Optional[str] = None,
    timeout_seconds: float = 30.0,
) -> LegalClassification:
    """Stage principal del legal_classifier.

    1. Hash del intent → busca en cache
    2. Cache HIT → devuelve
    3. Cache MISS → LLM (gpt-4o) + verifica citas con article_index
    4. Persiste en cache
    """
    started = time.time()
    if not intent or not isinstance(intent, str):
        return EMPTY_CLASSIFICATION

    normalized = _normalize_prompt(intent, doc_type_hint)
    prompt_hash = _hash_prompt(normalized)

    # 1. Cache lookup
    cached = await _cache_get(pool, prompt_hash)
    if cached is not None:
        cached.duration_ms = int((time.time() - started) * 1000)
        logger.info("legal_classifier: CACHE HIT for hash=%s family=%s",
                    prompt_hash[:8], cached.document_family)
        return cached

    # 2. Run with timeout
    try:
        async def _run():
            # Paralelo: LLM + verificación de citas
            llm_task = asyncio.create_task(_llm_classify(client, intent, doc_type_hint))
            citas_task = asyncio.create_task(_verify_user_citations_with_article_index(pool, intent))
            llm_data = await llm_task
            citas_verificadas = await citas_task

            if llm_data is None:
                return EMPTY_CLASSIFICATION

            # Construir classification
            premisas_raw = llm_data.get("premisas_corregidas") or []
            premisas = []
            for p in premisas_raw[:10]:
                if not isinstance(p, dict):
                    continue
                premisas.append(PremisaCorregida(
                    usuario_dijo=str(p.get("usuario_dijo", ""))[:200],
                    correcto=str(p.get("correcto", ""))[:400],
                    razon=str(p.get("razon", ""))[:600],
                    fuente=str(p.get("fuente", ""))[:200] or None,
                ))

            # Si article_index detectó citas inexistentes y el LLM no las puso
            # en premisas_corregidas, agregarlas como premisas automáticas.
            for cita in citas_verificadas:
                if cita.exists or not cita.suggested_correction:
                    continue
                # Evitar duplicar si ya está en premisas del LLM
                already = any(
                    cita.ref.lower() in p.usuario_dijo.lower()
                    for p in premisas
                )
                if not already:
                    premisas.append(PremisaCorregida(
                        usuario_dijo=cita.ref,
                        correcto=f"Artículo inexistente — máximo Art. {cita.max_in_law} en esa ley",
                        razon=cita.suggested_correction,
                        fuente=None,
                    ))

            adv_raw = llm_data.get("advertencias_riesgo") or []
            advertencias = [str(a)[:400] for a in adv_raw if isinstance(a, str)][:8]

            fundamento_raw = llm_data.get("fundamento_normativo") or []
            fundamento = [str(f)[:200] for f in fundamento_raw if isinstance(f, str)][:8]

            return LegalClassification(
                document_family=str(llm_data.get("document_family", "otro"))[:60],
                regimen_aplicable=str(llm_data.get("regimen_aplicable", ""))[:60] or None,
                naturaleza_acto=str(llm_data.get("naturaleza_acto", ""))[:60] or None,
                fundamento_normativo=fundamento,
                premisas_corregidas=premisas,
                advertencias_riesgo=advertencias,
                citas_verificadas=citas_verificadas,
                reasoning=str(llm_data.get("reasoning", ""))[:1000],
                cached=False,
            )

        result = await asyncio.wait_for(_run(), timeout=timeout_seconds)
        result.duration_ms = int((time.time() - started) * 1000)

        # 3. Persistir en cache (best-effort, no bloquea)
        try:
            await _cache_write(pool, prompt_hash, intent[:300], doc_type_hint, result)
        except Exception as e:
            logger.debug("legal_classifier cache write failed: %s", e)

        logger.info(
            "legal_classifier: classified family=%s regimen=%s premisas=%d advertencias=%d",
            result.document_family, result.regimen_aplicable,
            len(result.premisas_corregidas), len(result.advertencias_riesgo),
        )
        return result

    except asyncio.TimeoutError:
        logger.warning("legal_classifier TIMEOUT after %.1fs — using empty classification", timeout_seconds)
        r = EMPTY_CLASSIFICATION
        r.duration_ms = int((time.time() - started) * 1000)
        return r
    except Exception as e:
        logger.warning("legal_classifier stage exception (non-fatal): %s", e)
        r = EMPTY_CLASSIFICATION
        r.duration_ms = int((time.time() - started) * 1000)
        return r
