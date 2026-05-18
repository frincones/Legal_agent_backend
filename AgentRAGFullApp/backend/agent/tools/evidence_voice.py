"""Sprint 21 · Voice tools de Evidence Authenticity.

  · validate_identity(subject_kind, subject_id_kind, subject_id_value, subject_name?)
  · check_doc_consistency(matter_document_id, document_text)
  · score_evidence(matter_document_id, document_text, auto_run_*?)
"""

from __future__ import annotations

import logging
from typing import Optional

from agent.tools._ui_events import ui_data_changed

logger = logging.getLogger(__name__)


async def validate_identity_tool(args: dict, ctx: dict) -> dict:
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    if not firm_id:
        return {"error": "firm_id requerido"}
    subject_id_value = (args.get("subject_id_value") or args.get("cedula")
                         or args.get("nit") or args.get("id_value") or "").strip()
    # Parser del prompt: extrae cédula/NIT cuando el LLM no los pasa explícito.
    if not subject_id_value:
        import re
        prompt_str = (args.get("prompt") or ctx.get("user_prompt") or "")
        # NIT 860.123.456-7 o 860123456 o cédula 1234567890
        m = re.search(r"\b(?:nit|c[ée]dula|cc|cedula)\s*[:\.]?\s*([0-9.\-]{6,20})", prompt_str.lower())
        if m:
            subject_id_value = m.group(1).replace(".", "").replace("-", "").strip()
        else:
            # Fallback: cualquier número largo (7+ dígitos)
            m = re.search(r"\b(\d{7,15})\b", prompt_str)
            if m:
                subject_id_value = m.group(1)
    if not subject_id_value:
        return {"error": "Necesito subject_id_value (cédula/NIT)"}
    subject_kind = (args.get("subject_kind") or "persona").strip().lower()
    if subject_kind not in ("persona", "empresa"):
        subject_kind = "persona"
    subject_id_kind = (args.get("subject_id_kind") or ("nit" if subject_kind == "empresa" else "cedula")).strip().lower()
    subject_name = args.get("subject_name") or args.get("nombre")

    from agent.tools.evidence_validator import run_validation
    try:
        result = await run_validation(
            firm_id=firm_id,
            subject_kind=subject_kind,
            subject_id_kind=subject_id_kind,
            subject_id_value=subject_id_value,
            subject_name=subject_name,
            matter_id=matter_id,
            matter_document_id=args.get("matter_document_id"),
            user_id=user_id,
        )
        return {
            "id": result["id"],
            "status": result["status"],
            "summary": result["summary"],
            "mismatches_count": len(result["mismatches"]),
            "_ui_command": ui_data_changed(
                "evidence", matter_id=matter_id, firm_id=firm_id, op="create",
                extra={"verification_id": result["id"], "kind": "identity"},
            ),
        }
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.exception("validate_identity_tool failed")
        return {"error": f"Fallo: {e}"}


async def check_doc_consistency_tool(args: dict, ctx: dict) -> dict:
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    matter_document_id = (args.get("matter_document_id") or "").strip()
    document_text = (args.get("document_text") or "").strip()
    if not (firm_id and matter_document_id and document_text):
        return {"error": "Necesito firm_id, matter_document_id y document_text"}
    from agent.tools.inconsistency_detector import detect_inconsistencies_in_document
    try:
        result = await detect_inconsistencies_in_document(
            firm_id=firm_id, matter_document_id=matter_document_id,
            document_text=document_text, matter_id=matter_id, user_id=user_id,
        )
        return {
            "id": result["id"],
            "total_count": result["total_count"],
            "high_severity_count": result["high_severity_count"],
            "summary": result["summary"],
            "cached": result["cached"],
            "_ui_command": ui_data_changed(
                "evidence", matter_id=matter_id, firm_id=firm_id, op="create",
                extra={"inconsistency_id": result["id"], "kind": "consistency"},
            ),
        }
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.exception("check_doc_consistency_tool failed")
        return {"error": f"Fallo: {e}"}


async def score_evidence_tool(args: dict, ctx: dict) -> dict:
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    matter_document_id = (args.get("matter_document_id") or "").strip()
    document_text = (args.get("document_text") or "").strip()
    if not (firm_id and matter_document_id and document_text):
        return {"error": "Necesito matter_document_id y document_text"}
    from agent.tools.probative_scorer import compute_probative_score
    try:
        result = await compute_probative_score(
            firm_id=firm_id,
            matter_document_id=matter_document_id,
            document_text=document_text,
            matter_id=matter_id,
            user_id=user_id,
            mime_type=args.get("mime_type"),
            auto_run_validation=bool(args.get("auto_run_validation", False)),
            auto_run_inconsistencies=bool(args.get("auto_run_inconsistencies", True)),
            subject_kind=args.get("subject_kind"),
            subject_id_kind=args.get("subject_id_kind"),
            subject_id_value=args.get("subject_id_value"),
            subject_name=args.get("subject_name"),
        )
        return {
            "id": result["id"],
            "probative_score": result["probative_score"],
            "level": result["level"],
            "summary": result["summary"],
            "positive_factors_count": len(result["positive_factors"]),
            "negative_factors_count": len(result["negative_factors"]),
            "_ui_command": ui_data_changed(
                "evidence", matter_id=matter_id, firm_id=firm_id, op="create",
                extra={"score_id": result["id"], "kind": "probative_score"},
            ),
        }
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.exception("score_evidence_tool failed")
        return {"error": f"Fallo: {e}"}
