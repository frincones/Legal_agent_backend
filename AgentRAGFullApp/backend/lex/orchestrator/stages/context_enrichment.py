"""M19.22 — Context Enrichment Stage (pre-research estilo Claude).

Se ejecuta ANTES del classifier. Investiga las normas y jurisprudencia que
mencionó el usuario en su prompt, las verifica, detecta errores comunes
(citas mal aplicadas, datos imposibles) y produce un `EnrichedContext` que
mejora la calidad del resto del pipeline.

Reusa al 100%:
  - VerificationAgent.verify_batch() para verificar las citas
  - JudgeAgent (vía VerificationAgent) para decidir si una cita es válida
  - El mismo LLM client (gpt-4o-mini) que ya se usa en otros stages

Output: `EnrichedContext` con:
  - verified_citations: list[VerificationVerdict]  # citas del usuario verificadas
  - citations_corrections: list[CitationCorrection]  # "SU-440 no aplica → SU-087"
  - data_warnings: list[DataWarning]  # "salario integral imposible", etc.
  - suggested_doc_type: str  # inferido del análisis (no del prompt crudo)
  - suggested_doc_type_confidence: float
  - reasoning: str  # narrativa para el narrator

Si este stage falla, retorna EnrichedContext vacío (no rompe pipeline).

Costo: ~$0.003-0.005 por documento (3 LLM calls de gpt-4o-mini).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ============================================================
# Schemas
# ============================================================

@dataclass
class CitationCorrection:
    """Una cita mal aplicada por el usuario que debe corregirse."""
    original_ref: str                  # "SU-440/2021"
    issue: str                          # "no aplica a estabilidad laboral, trata de identidad de género"
    suggested_replacement: Optional[str] = None  # "SU-087/2022"
    suggested_replacement_reason: Optional[str] = None  # "es la sentencia hito reciente sobre estabilidad reforzada"
    confidence: float = 0.0


@dataclass
class DataWarning:
    """Una inconsistencia o dato imposible en lo que dijo el usuario."""
    field: str                          # "salario_integral" / "fecha_terminacion" / "norma_madre_cabeza"
    issue: str                          # "salario $4.850.000 no puede ser integral (mínimo legal 13 SMMLV ≈ $18.5M)"
    suggested_fix: Optional[str] = None  # "tratar como salario ordinario; declarar ineficaz la estipulación"
    severity: str = "warning"           # 'info' | 'warning' | 'critical'


@dataclass
class EnrichedContext:
    """Contexto enriquecido producido por ContextEnrichmentStage."""
    verified_citations: list[dict] = field(default_factory=list)       # to_audit_dict() de verdicts
    citations_corrections: list[CitationCorrection] = field(default_factory=list)
    data_warnings: list[DataWarning] = field(default_factory=list)
    suggested_doc_type: Optional[str] = None
    suggested_doc_type_confidence: float = 0.0
    suggested_jurisdiccion: Optional[str] = None
    reasoning: str = ""                  # narrativa breve para el narrator
    enrichment_skipped: bool = False     # true si el stage no se ejecutó
    duration_ms: int = 0

    @property
    def corrections_count(self) -> int:
        return len(self.citations_corrections) + len(self.data_warnings)

    @property
    def has_corrections(self) -> bool:
        return self.corrections_count > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified_citations_count": len(self.verified_citations),
            "citations_corrections": [asdict(c) for c in self.citations_corrections],
            "data_warnings": [asdict(w) for w in self.data_warnings],
            "suggested_doc_type": self.suggested_doc_type,
            "suggested_doc_type_confidence": self.suggested_doc_type_confidence,
            "suggested_jurisdiccion": self.suggested_jurisdiccion,
            "reasoning": self.reasoning,
            "enrichment_skipped": self.enrichment_skipped,
            "duration_ms": self.duration_ms,
        }

    def corrections_summary_for_narrator(self) -> str:
        """Texto plano para inyectar en el moment 'synthesis' del narrator."""
        if not self.has_corrections:
            return ""
        lines: list[str] = []
        for i, c in enumerate(self.citations_corrections, 1):
            line = f"{i}. {c.original_ref}: {c.issue}"
            if c.suggested_replacement:
                line += f" → sugerido reemplazar por {c.suggested_replacement}"
                if c.suggested_replacement_reason:
                    line += f" ({c.suggested_replacement_reason})"
            lines.append(line)
        offset = len(self.citations_corrections)
        for j, w in enumerate(self.data_warnings, 1):
            line = f"{offset + j}. [{w.field}] {w.issue}"
            if w.suggested_fix:
                line += f" — {w.suggested_fix}"
            lines.append(line)
        return "\n".join(lines)


EMPTY_ENRICHED_CONTEXT = EnrichedContext(enrichment_skipped=True)


# ============================================================
# LLM prompts
# ============================================================

CITATIONS_EXTRACTOR_PROMPT = """Eres un asistente legal colombiano. Tu tarea es extraer del prompt del usuario
las referencias normativas y jurisprudenciales que aparecen explícitamente
mencionadas, para que otro sistema las verifique.

NO inventes citas que no estén en el prompt. Solo extrae las que aparecen
literalmente (Art. X CST, Ley N/AAAA, T-NNN/AAAA, SU-NNN/AAAA, SL-NNNN-AAAA,
C-NNN/AAAA, etc.).

OUTPUT (JSON):
{
  "citations": [
    {"ref": "Art. 64 CST", "type": "norma"},
    {"ref": "SU-049/2017", "type": "jurisprudencia"},
    {"ref": "Ley 1010 de 2006", "type": "norma"},
    {"ref": "SC-4665/2021", "type": "jurisprudencia"}
  ]
}

Si no hay citas → {"citations": []}. Máximo 20 citas.
"""


INCONSISTENCY_DETECTOR_PROMPT = """Eres un ABOGADO LITIGANTE SENIOR colombiano con 25+ años de experiencia.
Tu trabajo es revisar un PROMPT DEL USUARIO (no un documento generado) antes
de redactar la demanda. Tu objetivo: detectar inconsistencias o errores
comunes que el usuario suele cometer al describir su caso.

Te paso:
  1. El prompt original del usuario.
  2. El brief adicional (si existe).
  3. Las citas que el usuario mencionó y el resultado de su verificación.

Devuelve DOS listas:

A) **citations_corrections**: citas mal aplicadas por el usuario.
   Ejemplos típicos:
   - "SU-440/2021" cuando el caso es estabilidad laboral
     (SU-440/21 es identidad de género; debe ser SU-087/22 o SU-049/17).
   - "Ley 1280/2009" como norma de madre cabeza de familia
     (Ley 1280/09 es licencia por luto; la correcta es Ley 82/93 modif. Ley 1232/08).
   - Sentencias del tipo equivocado para el caso.
   Solo incluye correcciones de ALTA confidence (≥0.75).

B) **data_warnings**: datos imposibles o inconsistentes en el caso narrado.
   Ejemplos típicos:
   - "salario integral $4.850.000" — el piso legal del Art. 132 CST es
     13 SMMLV (≈ $18.5M en 2025). Un salario menor NO puede ser integral.
   - "vínculo laboral de 30 años" pero "ingresó en 2020" → contradicción.
   - "menor de 12 años con responsabilidad penal directa" → imposible.
   - "demanda ejecutiva sin título" → falta requisito esencial.
   - "tutela como acción principal" cuando hay otro mecanismo idóneo.
   - Fechas absurdas (fecha de terminación anterior a fecha de inicio, etc.).
   - Cuantía declarada que no corresponde a la jurisdicción.

REGLAS:
- NO inventes problemas. Si todo es consistente, retorna listas vacías.
- Solo flaggea problemas REALES y verificables.
- Cada corrección debe ser ACCIONABLE: explica qué hacer.
- severity: 'critical' (procesalmente fatal), 'warning' (importante), 'info'.

OUTPUT (JSON):
{
  "citations_corrections": [
    {
      "original_ref": "SU-440/2021",
      "issue": "no aplica a estabilidad laboral, trata de identidad de género",
      "suggested_replacement": "SU-087/2022",
      "suggested_replacement_reason": "es la sentencia hito reciente sobre estabilidad reforzada",
      "confidence": 0.95
    }
  ],
  "data_warnings": [
    {
      "field": "salario_integral",
      "issue": "salario $4.850.000 no puede ser integral (mínimo legal 13 SMMLV ≈ $18.5M)",
      "suggested_fix": "tratar como salario ordinario; declarar ineficaz la estipulación de salario integral; reclamar cesantías/intereses/primas autónomamente",
      "severity": "warning"
    }
  ]
}
"""


DOC_TYPE_SUGGESTER_PROMPT = """Eres un clasificador legal experto colombiano. Tu única tarea es decidir
el doc_type correcto del documento que el usuario quiere generar, basándote
en el PROMPT COMPLETO + el análisis previo (citas verificadas + correcciones
detectadas).

Lista cerrada de doc_types permitidos:
  demanda_laboral_ordinaria, demanda_civil_ordinaria, demanda_ejecutivo_singular,
  demanda_pertenencia, demanda_responsabilidad_civil, demanda_alimentos,
  demanda_divorcio, demanda_nulidad_restablecimiento, demanda_simple_nulidad,
  demanda_reparacion_directa, tutela, derecho_peticion, accion_popular,
  accion_grupo, contrato_arrendamiento, contrato_prestacion_servicios,
  denuncia_penal, recurso_apelacion, concepto_juridico, poder_especial.

REGLAS CRÍTICAS DE ELECCIÓN:
- Si el caso es CONTRA ENTIDAD PÚBLICA (DIAN, SIC, Alcaldía, Ministerio,
  Superintendencia) por acto administrativo, JAMÁS es "demanda_civil_ordinaria"
  → es "demanda_nulidad_restablecimiento" (o simple_nulidad si no hay
  pretensión patrimonial, o reparacion_directa si no hay acto).
- Si menciona "divorcio" o "separación de cuerpos" → "demanda_divorcio"
  (NUNCA "civil_ordinaria" aunque técnicamente sea verbal civil).
- Si menciona "prescripción adquisitiva" / "usucapión" / "pertenencia"
  → "demanda_pertenencia".
- Si menciona "accidente de tránsito" / "responsabilidad extracontractual"
  → "demanda_responsabilidad_civil".
- Si menciona "tutela" / "amparo" → "tutela".
- Si menciona "alimentos" / "cuota alimentaria" → "demanda_alimentos".
- Si menciona "despido" / "fuero" / "contrato realidad" / "cesantías"
  → "demanda_laboral_ordinaria".
- Solo usa "demanda_civil_ordinaria" cuando NINGUNO de los específicos
  aplique con confidence ≥0.7.

OUTPUT (JSON):
{
  "doc_type": "demanda_divorcio",
  "jurisdiccion": "familia",
  "confidence": 0.92,
  "reasoning": "el usuario explícitamente pide divorcio contencioso por causal del Art. 154 num. 1 y 2 CC"
}
"""


# ============================================================
# Stage principal
# ============================================================

async def enrich_context(
    client,
    pool,
    intent: str,
    brief: str | None = None,
    firm_id: str | None = None,
    user_id: str | None = None,
    timeout_seconds: float = 60.0,
) -> EnrichedContext:
    """Punto de entrada principal. El orchestrator lo invoca ANTES del classifier.

    Si cualquier paso falla, retorna un EnrichedContext vacío en lugar de
    levantar excepción — el pipeline debe poder continuar siempre.
    """
    started = time.time()
    import asyncio

    try:
        result = await asyncio.wait_for(
            _enrich_context_inner(client, pool, intent, brief, firm_id, user_id),
            timeout=timeout_seconds,
        )
        result.duration_ms = int((time.time() - started) * 1000)
        return result
    except asyncio.TimeoutError:
        logger.warning("context_enrichment TIMEOUT after %.1fs — skipping", timeout_seconds)
        return EnrichedContext(
            enrichment_skipped=True,
            reasoning="context_enrichment timed out, pipeline continúa sin contexto enriquecido",
            duration_ms=int((time.time() - started) * 1000),
        )
    except Exception as e:
        logger.warning("context_enrichment failed (non-fatal): %s", e)
        return EnrichedContext(
            enrichment_skipped=True,
            reasoning=f"context_enrichment falló: {str(e)[:120]}",
            duration_ms=int((time.time() - started) * 1000),
        )


async def _enrich_context_inner(
    client, pool, intent: str, brief: str | None,
    firm_id: str | None, user_id: str | None,
) -> EnrichedContext:
    """Implementación interna (envuelta por enrich_context con timeout)."""

    # PASO 1: Extraer citas del prompt
    citas = await _extract_citas_from_prompt(client, intent, brief)
    logger.info("context_enrichment: extracted %d citas from prompt", len(citas))

    # PASO 2: Verificar citas con VerificationAgent (REUSA)
    verified_dicts: list[dict] = []
    if citas and pool is not None:
        try:
            from lex.verify.verification_agent import VerificationAgent
            verifier = VerificationAgent(
                client=client,
                pool=pool,
                firm_id=firm_id,
                user_id=user_id,
            )
            verdicts = await verifier.verify_batch(citas)
            verified_dicts = [v.to_audit_dict() for v in verdicts]
            logger.info(
                "context_enrichment: verified %d/%d citas",
                sum(1 for v in verdicts if v.verified), len(verdicts),
            )
        except Exception as e:
            logger.warning("context_enrichment verify_batch failed (non-fatal): %s", e)

    # PASO 3: Detectar inconsistencias en datos + citas mal aplicadas
    citations_corrections, data_warnings = await _detect_inconsistencies(
        client, intent, brief, verified_dicts
    )

    # M19.24.E.2 — Validar Art. X dentro de Ley Y usando article_index
    # Detecta el clásico "Art. 836 CGP" (no existe, CGP llega al 627)
    try:
        from lex.verify.article_index import verify_article_batch
        # Recopilar citas tipo "Art. X Y" del intent + brief + verified
        import re as _re
        intent_text = (intent or "") + " " + (brief or "")
        article_refs: list[str] = []
        seen = set()
        for m in _re.finditer(
            r"art(?:[íi]culo|\.?)\s+\d{1,5}\s+[A-Za-zÁÉÍÓÚáéíóúñÑ\.\s]{2,60}",
            intent_text,
            _re.IGNORECASE,
        ):
            candidate = _re.sub(r"\s+", " ", m.group(0).strip())[:80]
            if candidate.lower() not in seen:
                seen.add(candidate.lower())
                article_refs.append(candidate)
            if len(article_refs) >= 10:
                break
        if article_refs and pool is not None:
            verdicts = await verify_article_batch(pool, article_refs)
            for v in verdicts:
                if v.parse_ok and not v.exists and v.suggested_correction:
                    # Añadir como CitationCorrection
                    try:
                        citations_corrections.append(CitationCorrection(
                            original_ref=v.cita_original,
                            issue=f"Artículo inexistente — el máximo de {v.ley_resolved or 'esa ley'} es {v.max_articulo_in_law}",
                            suggested_replacement=None,
                            suggested_replacement_reason=v.suggested_correction[:300],
                            confidence=0.95,
                        ))
                    except Exception:
                        pass
    except Exception as e:
        logger.debug("article_index validation in context_enrichment failed: %s", e)

    logger.info(
        "context_enrichment: %d citation corrections, %d data warnings",
        len(citations_corrections), len(data_warnings),
    )

    # PASO 4: Sugerir doc_type basado en todo el contexto enriquecido
    doc_type, jurisdiccion, confidence, reasoning = await _suggest_doc_type(
        client, intent, brief, verified_dicts, citations_corrections, data_warnings,
    )
    logger.info(
        "context_enrichment: suggested doc_type=%s jurisdiccion=%s confidence=%.2f",
        doc_type, jurisdiccion, confidence,
    )

    return EnrichedContext(
        verified_citations=verified_dicts,
        citations_corrections=citations_corrections,
        data_warnings=data_warnings,
        suggested_doc_type=doc_type,
        suggested_doc_type_confidence=confidence,
        suggested_jurisdiccion=jurisdiccion,
        reasoning=reasoning,
        enrichment_skipped=False,
    )


# ============================================================
# Sub-stages (LLM calls)
# ============================================================

async def _extract_citas_from_prompt(client, intent: str, brief: str | None) -> list[dict]:
    """LLM extrae citas mencionadas en el prompt."""
    if client is None:
        return []
    try:
        user_msg = f"PROMPT DEL USUARIO:\n{intent}\n\nBRIEF ADICIONAL:\n{brief or '(sin brief)'}\n\nExtrae las citas mencionadas como JSON."
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": CITATIONS_EXTRACTOR_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.0,
            max_tokens=800,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        citas_raw = data.get("citations") or []
        citas: list[dict] = []
        for c in citas_raw[:20]:
            if not isinstance(c, dict):
                continue
            ref = str(c.get("ref", "")).strip()
            ctype = str(c.get("type", "norma")).strip().lower()
            if not ref or len(ref) > 200:
                continue
            if ctype not in ("norma", "jurisprudencia"):
                ctype = "norma"
            citas.append({"ref": ref, "type": ctype})
        return citas
    except Exception as e:
        logger.warning("_extract_citas_from_prompt failed: %s", e)
        return []


async def _detect_inconsistencies(
    client, intent: str, brief: str | None, verified_dicts: list[dict],
) -> tuple[list[CitationCorrection], list[DataWarning]]:
    """LLM-as-judge detecta citas mal aplicadas y datos imposibles."""
    if client is None:
        return [], []
    try:
        # Resumen de verificación para el LLM
        verified_summary = []
        for v in verified_dicts[:20]:
            ref = v.get("ref", "?")
            estado = v.get("estado", "?")
            verified_summary.append(f"  - {ref} [{estado}]")
        verified_block = "\n".join(verified_summary) if verified_summary else "  (sin citas verificadas)"

        user_msg = f"""PROMPT DEL USUARIO:
{intent}

BRIEF ADICIONAL:
{brief or '(sin brief)'}

CITAS QUE EL USUARIO MENCIONÓ Y SU VERIFICACIÓN:
{verified_block}

Devuelve JSON {{citations_corrections: [...], data_warnings: [...]}} estrictamente como en el system prompt."""

        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": INCONSISTENCY_DETECTOR_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.1,
            max_tokens=1500,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)

        corrections: list[CitationCorrection] = []
        for c in (data.get("citations_corrections") or [])[:10]:
            if not isinstance(c, dict):
                continue
            conf = c.get("confidence", 0.0)
            try:
                conf = float(conf)
            except Exception:
                conf = 0.0
            # Solo agregar correcciones de confidence >= 0.75
            if conf < 0.75:
                continue
            corrections.append(CitationCorrection(
                original_ref=str(c.get("original_ref", ""))[:200],
                issue=str(c.get("issue", ""))[:400],
                suggested_replacement=(str(c.get("suggested_replacement", ""))[:200] or None),
                suggested_replacement_reason=(str(c.get("suggested_replacement_reason", ""))[:300] or None),
                confidence=conf,
            ))

        warnings: list[DataWarning] = []
        for w in (data.get("data_warnings") or [])[:10]:
            if not isinstance(w, dict):
                continue
            sev = str(w.get("severity", "warning"))
            if sev not in ("critical", "warning", "info"):
                sev = "warning"
            warnings.append(DataWarning(
                field=str(w.get("field", "unknown"))[:60],
                issue=str(w.get("issue", ""))[:400],
                suggested_fix=(str(w.get("suggested_fix", ""))[:400] or None),
                severity=sev,
            ))

        return corrections, warnings
    except Exception as e:
        logger.warning("_detect_inconsistencies failed: %s", e)
        return [], []


async def _suggest_doc_type(
    client, intent: str, brief: str | None,
    verified_dicts: list[dict],
    corrections: list[CitationCorrection],
    warnings: list[DataWarning],
) -> tuple[Optional[str], Optional[str], float, str]:
    """LLM sugiere doc_type con contexto enriquecido. Devuelve
    (doc_type, jurisdiccion, confidence, reasoning)."""
    if client is None:
        return None, None, 0.0, ""
    try:
        ctx_lines: list[str] = []
        if verified_dicts:
            ctx_lines.append("CITAS DEL USUARIO (verificadas):")
            for v in verified_dicts[:15]:
                ctx_lines.append(f"  - {v.get('ref', '?')} [{v.get('estado', '?')}]")
        if corrections:
            ctx_lines.append("\nCORRECCIONES DETECTADAS EN LAS CITAS:")
            for c in corrections[:5]:
                ctx_lines.append(f"  - {c.original_ref} → {c.suggested_replacement or '(reemplazo no propuesto)'}: {c.issue}")
        if warnings:
            ctx_lines.append("\nADVERTENCIAS SOBRE DATOS:")
            for w in warnings[:5]:
                ctx_lines.append(f"  - [{w.field}] {w.issue}")
        ctx_block = "\n".join(ctx_lines) if ctx_lines else "(sin análisis previo disponible)"

        user_msg = f"""PROMPT DEL USUARIO:
{intent}

BRIEF ADICIONAL:
{brief or '(sin brief)'}

CONTEXTO ENRIQUECIDO (análisis previo):
{ctx_block}

Elige el doc_type correcto. Devuelve JSON con keys: doc_type, jurisdiccion, confidence, reasoning."""

        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": DOC_TYPE_SUGGESTER_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.0,
            max_tokens=300,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        doc_type = str(data.get("doc_type", "")).strip().lower() or None
        jurisdiccion = str(data.get("jurisdiccion", "")).strip().lower() or None
        confidence = data.get("confidence", 0.0)
        try:
            confidence = float(confidence)
        except Exception:
            confidence = 0.0
        reasoning = str(data.get("reasoning", ""))[:500]
        return doc_type, jurisdiccion, confidence, reasoning
    except Exception as e:
        logger.warning("_suggest_doc_type failed: %s", e)
        return None, None, 0.0, ""
