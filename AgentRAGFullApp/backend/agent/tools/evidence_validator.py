"""Sprint 21 · Evidence validator · cross-check de identidad.

Recibe: { subject_kind, subject_id_kind, subject_id_value, subject_name }
Llama a los providers correspondientes:
  - persona + cedula → registro_civil + rut
  - empresa + nit    → rue + rut
Consolida resultados, detecta mismatches y persiste.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


VALID_SUBJECT_KINDS = {"persona", "empresa"}
VALID_ID_KINDS = {"cedula", "nit", "pasaporte", "rut", "otro"}


def _pick_providers(subject_kind: str, subject_id_kind: str) -> list[str]:
    """Decide qué providers consultar según el tipo de sujeto."""
    providers: list[str] = []
    if subject_kind == "persona":
        if subject_id_kind == "cedula":
            providers.append("registro_civil")
        providers.append("rut")  # cualquier persona puede tener RUT
    elif subject_kind == "empresa":
        if subject_id_kind == "nit":
            providers.append("rue")
        providers.append("rut")
    return providers


async def run_validation(
    firm_id: str,
    subject_kind: str,
    subject_id_kind: str,
    subject_id_value: str,
    subject_name: Optional[str] = None,
    matter_id: Optional[str] = None,
    matter_document_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> dict:
    """Corre la validación cruzada + persiste + devuelve resultado."""
    from utils.db import get_storage
    from utils.identity_providers import (
        validate_registro_civil, validate_rue, validate_rut,
        normalize_id, normalize_name, name_similarity,
    )

    if subject_kind not in VALID_SUBJECT_KINDS:
        raise ValueError(f"subject_kind inválido (válidos: {sorted(VALID_SUBJECT_KINDS)})")
    if subject_id_kind not in VALID_ID_KINDS:
        raise ValueError(f"subject_id_kind inválido (válidos: {sorted(VALID_ID_KINDS)})")

    id_norm = normalize_id(subject_id_value)
    if not id_norm:
        raise ValueError("subject_id_value vacío")

    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise ValueError("Storage no disponible")

    providers_to_run = _pick_providers(subject_kind, subject_id_kind)
    results: dict[str, dict] = {}
    mismatches: list[dict] = []

    # Ejecutar cada provider
    for p in providers_to_run:
        try:
            if p == "registro_civil":
                results[p] = await validate_registro_civil(id_norm, subject_name or "")
            elif p == "rue":
                results[p] = await validate_rue(id_norm, subject_name or "")
            elif p == "rut":
                results[p] = await validate_rut(id_norm)
        except Exception as e:
            logger.warning("provider %s failed: %s", p, e)
            results[p] = {
                "provider": p, "ok": False, "status": "error",
                "error": str(e)[:300],
            }

    # Detectar mismatches estructurados
    expected_name = normalize_name(subject_name or "")
    for p, r in results.items():
        if r.get("status") == "mismatch":
            payload = r.get("payload") or {}
            official = (
                payload.get("nombre_completo_oficial")
                or payload.get("razon_social_oficial")
                or "(no expuesto)"
            )
            mismatches.append({
                "provider": p,
                "field": "nombre" if subject_kind == "persona" else "razon_social",
                "expected": subject_name or "",
                "found": official,
                "severity": "high",
            })
        elif r.get("status") == "partial":
            payload = r.get("payload") or {}
            official = (
                payload.get("nombre_completo_oficial")
                or payload.get("razon_social_oficial")
                or ""
            )
            sim = name_similarity(expected_name, official) if (expected_name and official) else 0.0
            if sim < 0.7 and (expected_name or official):
                mismatches.append({
                    "provider": p,
                    "field": "nombre" if subject_kind == "persona" else "razon_social",
                    "expected": subject_name or "",
                    "found": official,
                    "similarity": sim,
                    "severity": "medium",
                })
        elif r.get("status") == "not_found":
            mismatches.append({
                "provider": p,
                "field": "id",
                "expected": id_norm,
                "found": "no encontrado",
                "severity": "high",
            })

    # Decidir status global
    statuses = {r.get("status") for r in results.values()}
    if statuses == {"matched"} or (
        "matched" in statuses and not (statuses & {"mismatch", "not_found"})
    ):
        global_status = "matched"
    elif "mismatch" in statuses:
        global_status = "mismatch"
    elif "not_found" in statuses:
        global_status = "not_found"
    elif "partial" in statuses:
        global_status = "partial"
    else:
        global_status = "error"

    # Persistir
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into evidence_validations
              (firm_id, matter_id, matter_document_id, subject_kind, subject_id_kind,
               subject_id_value, subject_name, providers_used, status,
               results, mismatches, validated_by)
            values ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7,
                    $8::text[], $9, $10::jsonb, $11::jsonb, $12::uuid)
            returning id, created_at
            """,
            firm_id, matter_id, matter_document_id,
            subject_kind, subject_id_kind, id_norm, subject_name,
            providers_to_run, global_status,
            json.dumps(results), json.dumps(mismatches),
            user_id,
        )

    return {
        "id": str(row["id"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "subject_kind": subject_kind,
        "subject_id_kind": subject_id_kind,
        "subject_id_value": id_norm,
        "subject_name": subject_name,
        "providers_used": providers_to_run,
        "status": global_status,
        "results": results,
        "mismatches": mismatches,
        "summary": _summarize(global_status, mismatches, results),
    }


def _summarize(global_status: str, mismatches: list, results: dict) -> str:
    if global_status == "matched":
        provs = ", ".join(results.keys())
        return f"Identidad validada · datos coinciden con {provs}."
    if global_status == "mismatch":
        return f"⚠ Inconsistencias detectadas en {len(mismatches)} campo(s) · revisar."
    if global_status == "not_found":
        return "✗ No encontrado en uno o más registros oficiales."
    if global_status == "partial":
        return "~ Coincidencia parcial · verificar manualmente."
    return "Error consultando registros · intenta de nuevo."
