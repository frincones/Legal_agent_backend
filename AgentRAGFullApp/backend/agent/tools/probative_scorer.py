"""Sprint 21 · Probative scorer · combina validation + inconsistencies + features.

Computa un score 0-100 con:
  +20 por cada validación matched
  +15 si el doc está notariado o tiene sello oficial
  +10 si está firmado (mencionado)
  +10 si es PDF (vs Word editable)
  +10 si menciona normas (no es solo narrativa)
  +5  si tiene fecha clara y radicado/consecutivo
  +5  si fuente oficial (Ministerio/Superintendencia/etc.)

Penalizaciones:
  -25 por cada inconsistency HIGH
  -10 por cada inconsistency MEDIUM
  -3  por cada inconsistency LOW
  -20 si hay validation mismatch
  -10 si hay validation partial
  -15 si validation not_found (cédula no existe)

El resultado se persiste en evidence_scores con factor breakdown auditable.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def compute_probative_score(
    firm_id: str,
    matter_document_id: str,
    document_text: str,
    matter_id: Optional[str] = None,
    user_id: Optional[str] = None,
    mime_type: Optional[str] = None,
    metadata: Optional[dict] = None,
    auto_run_validation: bool = False,
    auto_run_inconsistencies: bool = True,
    subject_kind: Optional[str] = None,
    subject_id_kind: Optional[str] = None,
    subject_id_value: Optional[str] = None,
    subject_name: Optional[str] = None,
) -> dict:
    """Computa score combinando latest validation + inconsistencies + features.

    Si `auto_run_validation` y se dan datos del sujeto, corre validation primero.
    Si `auto_run_inconsistencies`, corre detector LLM primero.
    """
    from utils.db import get_storage
    from utils.evidence_helpers import classify_doc_features, score_level

    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise ValueError("Storage no disponible")

    validation_row = None
    inconsistency_row = None

    # 1. (Opcional) Correr validation
    if auto_run_validation and subject_id_value:
        from agent.tools.evidence_validator import run_validation
        try:
            v = await run_validation(
                firm_id=firm_id,
                subject_kind=subject_kind or "persona",
                subject_id_kind=subject_id_kind or "cedula",
                subject_id_value=subject_id_value,
                subject_name=subject_name,
                matter_id=matter_id,
                matter_document_id=matter_document_id,
                user_id=user_id,
            )
            async with storage.pool.acquire() as conn:
                validation_row = await conn.fetchrow(
                    "select id, status, mismatches from evidence_validations where id = $1::uuid",
                    v["id"],
                )
        except Exception as e:
            logger.warning("auto validation failed: %s", e)

    # 2. (Opcional) Correr inconsistencies
    if auto_run_inconsistencies:
        from agent.tools.inconsistency_detector import detect_inconsistencies_in_document
        try:
            i = await detect_inconsistencies_in_document(
                firm_id=firm_id,
                matter_document_id=matter_document_id,
                document_text=document_text,
                matter_id=matter_id,
                user_id=user_id,
            )
            async with storage.pool.acquire() as conn:
                inconsistency_row = await conn.fetchrow(
                    "select id, inconsistencies, total_count, high_severity_count "
                    "from evidence_inconsistencies where id = $1::uuid",
                    i["id"],
                )
        except Exception as e:
            logger.warning("auto inconsistency failed: %s", e)

    # 3. Si no se corrieron, tomar las más recientes
    async with storage.pool.acquire() as conn:
        if validation_row is None:
            try:
                validation_row = await conn.fetchrow(
                    """
                    select id, status, mismatches from evidence_validations
                     where firm_id = $1::uuid and matter_document_id = $2::uuid
                     order by created_at desc limit 1
                    """,
                    firm_id, matter_document_id,
                )
            except Exception:
                pass
        if inconsistency_row is None:
            inconsistency_row = await conn.fetchrow(
                """
                select id, inconsistencies, total_count, high_severity_count
                  from evidence_inconsistencies
                 where firm_id = $1::uuid and matter_document_id = $2::uuid
                 order by analyzed_at desc limit 1
                """,
                firm_id, matter_document_id,
            )

    # 4. Features del doc
    features = classify_doc_features(document_text, mime_type=mime_type, metadata=metadata)

    # 5. Computar score
    score, positives, negatives, recs = _compute(features, validation_row, inconsistency_row)
    score = max(0, min(100, score))
    level = score_level(score)
    summary = _summarize(score, level, positives, negatives)

    validation_id = validation_row["id"] if validation_row else None
    inconsistency_id = inconsistency_row["id"] if inconsistency_row else None

    # 6. Persistir
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into evidence_scores
              (firm_id, matter_id, matter_document_id, probative_score, level,
               summary, positive_factors, negative_factors, recommendations,
               validation_id, inconsistency_id, computed_by)
            values ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6,
                    $7::jsonb, $8::jsonb, $9::jsonb,
                    $10::uuid, $11::uuid, $12::uuid)
            returning id, computed_at
            """,
            firm_id, matter_id, matter_document_id, score, level, summary,
            json.dumps(positives), json.dumps(negatives), json.dumps(recs),
            validation_id, inconsistency_id, user_id,
        )

    return {
        "id": str(row["id"]),
        "computed_at": row["computed_at"].isoformat() if row["computed_at"] else None,
        "probative_score": score,
        "level": level,
        "summary": summary,
        "positive_factors": positives,
        "negative_factors": negatives,
        "recommendations": recs,
        "validation_id": str(validation_id) if validation_id else None,
        "inconsistency_id": str(inconsistency_id) if inconsistency_id else None,
        "features": features,
    }


def _compute(features: dict, validation_row, inconsistency_row) -> tuple[int, list, list, list]:
    score = 30  # base
    positives: list[dict] = []
    negatives: list[dict] = []
    recs: list[str] = []

    # Positives · features
    if features.get("has_notarial"):
        score += 15
        positives.append({"factor": "notariado", "weight": 15,
                          "note": "Documento menciona protocolización notarial · fe pública."})
    if features.get("has_official_stamp"):
        score += 10
        positives.append({"factor": "sello oficial", "weight": 10,
                          "note": "Menciona sello oficial · refuerza autenticidad."})
    if features.get("has_signature_mention"):
        score += 10
        positives.append({"factor": "firma", "weight": 10,
                          "note": "Firmas mencionadas explícitamente."})
    if features.get("is_pdf"):
        score += 10
        positives.append({"factor": "formato PDF", "weight": 10,
                          "note": "PDF es menos editable que Word · prefiere PDF para evidencia."})
    else:
        recs.append("Convertir el documento a PDF/A antes de presentarlo.")
    if features.get("has_legal_refs"):
        score += 10
        positives.append({"factor": "referencias normativas", "weight": 10,
                          "note": "Cita normas concretas · facilita verificación."})
    if features.get("has_date_reference"):
        score += 5
        positives.append({"factor": "fecha clara", "weight": 5,
                          "note": "Fecha del documento identificable."})
    else:
        negatives.append({"factor": "sin fecha clara", "weight": -5,
                          "note": "No se identificó fecha explícita."})
        score -= 5
    if features.get("has_radicado"):
        score += 5
        positives.append({"factor": "número de radicado", "weight": 5,
                          "note": "Tiene radicado / consecutivo · trazable."})
    if features.get("is_official_source"):
        score += 5
        positives.append({"factor": "fuente oficial", "weight": 5,
                          "note": "Menciona entidad oficial (Ministerio, Superintendencia, etc.)."})

    # Validation impact
    if validation_row:
        st = validation_row["status"]
        if st == "matched":
            score += 20
            positives.append({"factor": "identidad validada", "weight": 20,
                              "note": "Identidad cruzada con registros oficiales."})
        elif st == "mismatch":
            score -= 20
            negatives.append({"factor": "identidad NO coincide", "weight": -20,
                              "note": "Nombre/empresa no coincide con registros oficiales."})
            recs.append("Reconciliar datos del sujeto con el registro oficial antes de presentar.")
        elif st == "partial":
            score -= 10
            negatives.append({"factor": "identidad parcial", "weight": -10,
                              "note": "Coincidencia parcial · revisar diferencias menores."})
        elif st == "not_found":
            score -= 15
            negatives.append({"factor": "identidad no encontrada", "weight": -15,
                              "note": "Cédula/NIT no aparece en registros consultados."})
            recs.append("Verificar manualmente con la entidad emisora.")

    # Inconsistencies impact
    if inconsistency_row:
        total = int(inconsistency_row["total_count"] or 0)
        high = int(inconsistency_row["high_severity_count"] or 0)
        incs = inconsistency_row["inconsistencies"]
        if isinstance(incs, str):
            try:
                incs = json.loads(incs)
            except Exception:
                incs = []
        med = sum(1 for x in (incs or []) if x.get("severity") == "medium")
        low = total - high - med

        if high > 0:
            score -= 25 * min(high, 3)  # cap at 75 penalty
            negatives.append({"factor": f"{high} inconsistencia(s) críticas", "weight": -25 * min(high, 3),
                              "note": f"Detectadas {high} inconsistencias HIGH · revisar urgente."})
            recs.append("Corregir cada inconsistencia HIGH antes de presentar.")
        if med > 0:
            score -= 10 * min(med, 5)
            negatives.append({"factor": f"{med} inconsistencia(s) medias", "weight": -10 * min(med, 5),
                              "note": f"Detectadas {med} inconsistencias MEDIUM · cuestionables."})
        if low > 0:
            score -= 3 * min(low, 5)
            negatives.append({"factor": f"{low} inconsistencia(s) menores", "weight": -3 * min(low, 5),
                              "note": f"Detectadas {low} inconsistencias LOW · pulir."})

    if not positives:
        recs.append("Verificar manualmente la autenticidad del documento antes de presentar.")
    if score < 60 and not any("notari" in p["factor"].lower() for p in positives):
        recs.append("Considerar protocolizar/notariar el documento para reforzar fe pública.")

    return score, positives, negatives, recs[:6]


def _summarize(score: int, level: str, positives: list, negatives: list) -> str:
    if level == "fuerte":
        return f"Evidencia FUERTE ({score}/100) · documento sólido para presentar."
    if level == "medio":
        return f"Evidencia MEDIA ({score}/100) · refuerza puntos débiles antes de presentar."
    if level == "debil":
        return f"Evidencia DÉBIL ({score}/100) · alto riesgo de cuestionamiento por la contraparte."
    return f"Evidencia CUESTIONABLE ({score}/100) · NO recomendado presentar sin correcciones."
