"""GET /v1/voice/tools — catalog of all registered voice tools for CommandPaletteV2.

Reads the live _tool_registry + _tool_descriptors() from api/voice.py at
request time. No DB query required: the registry is populated in memory
during the lifespan startup in main.py.

Auth: Supabase JWT with firm_id (same as /v1/skills/execute).
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/voice", tags=["voice"])

# ---------------------------------------------------------------------------
# Category inference — driven by tool name prefix / module hints.
# Keep this simple (no external deps) so startup stays fast.
# ---------------------------------------------------------------------------

_NAME_TO_CATEGORY: dict[str, str] = {
    # notes
    "add_matter_note": "notes",
    "list_matter_notes": "notes",
    # tasks / productivity
    "create_task": "tasks",
    "complete_task": "tasks",
    "what_today": "tasks",
    "what_is_my_priority": "tasks",
    # matters
    "create_matter": "matters",
    "archive_matter": "matters",
    "set_matter_priority": "matters",
    "tag_matter": "matters",
    "update_matter_etapa": "matters",
    "open_matter_context": "matters",
    "list_my_matters": "matters",
    "add_matter_deadline": "matters",
    "mark_deadline_done": "matters",
    "list_upcoming_deadlines": "matters",
    # documents
    "list_matter_documents": "documents",
    "summarize_document": "documents",
    "extract_document_entities": "documents",
    "get_document_content": "documents",
    "analyze_contract": "documents",
    "ask_about_document": "documents",
    "compare_documents": "documents",
    "autofill_template": "documents",
    "extract_variables_from_text": "documents",
    "list_legal_templates": "documents",
    "draft_pleading": "documents",
    "review_contract": "documents",
    "apply_redline": "documents",
    "reject_redline": "documents",
    # time & billing
    "track_time": "time_and_billing",
    "log_expense": "time_and_billing",
    "generate_invoice": "time_and_billing",
    "check_trust_balance": "time_and_billing",
    "record_trust_deposit": "time_and_billing",
    "record_trust_payment": "time_and_billing",
    "current_plan_status": "time_and_billing",
    "remaining_quota": "time_and_billing",
    "pricing_recommendation": "time_and_billing",
    # evidence
    "score_evidence": "evidence",
    "check_doc_consistency": "evidence",
    "validate_identity": "evidence",
    # legal_search
    "research_jurisprudence": "legal_search",
    "validate_citation": "legal_search",
    "validate_norm_vigencia": "legal_search",
    "search_suin_juriscol": "legal_search",
    "fetch_dof_co_publicacion": "legal_search",
    "fetch_banrep_dtf": "legal_search",
    "verify_rue_persona": "legal_search",
    "find_anything": "legal_search",
    # judges
    "search_judge": "judges",
    "get_judge_stats": "judges",
    "simulate_judge_view": "judges",
    # wizards
    "list_wizards": "wizards",
    "start_wizard": "wizards",
    "wizard_session_status": "wizards",
    # outcomes / predictions
    "predict_outcome": "outcomes",
    "extract_lesson": "outcomes",
    "search_lessons": "outcomes",
    # canvas
    "canvas_set_text": "canvas",
    "canvas_append": "canvas",
    "canvas_replace_section": "canvas",
    "canvas_save_version": "canvas",
    "canvas_get_current": "canvas",
    "canvas_insert_at_cursor": "canvas",
    "canvas_find_replace": "canvas",
    "canvas_select_section": "canvas",
    # comments & kb
    "add_comment": "comments_kb",
    "resolve_comment": "comments_kb",
    "search_kb": "comments_kb",
    "add_to_kb": "comments_kb",
    # clients / identity
    "find_client": "clients",
    "get_firm_metrics": "clients",
    # calc
    "calc_liquidacion": "calculations",
    "calc_prescripcion": "calculations",
    "calc_intereses": "calculations",
    # analytics
    "firm_revenue": "analytics",
    "lawyer_performance": "analytics",
    "prediction_accuracy": "analytics",
    "executive_kpis": "analytics",
    "analyze_firm_performance": "analytics",
    # admin / saas
    "saas_mrr_now": "admin",
    "saas_signups_mtd": "admin",
    "saas_churn_30d": "admin",
    "search_firm_by_name": "admin",
    "firm_health_snapshot": "admin",
    # HITL
    "request_human_approval": "hitl",
    "list_pending_hitl": "hitl",
    # UI bridge
    "ui_navigate": "ui",
    "ui_open_matter_canvas": "ui",
    "ui_open_matter_tab": "ui",
    "ui_scroll_to": "ui",
    "ui_open_command_palette": "ui",
    "ui_prefill_form": "ui",
    "ui_show_toast": "ui",
    "ui_open_modal": "ui",
    # memory
    "remember": "memory",
    "recall": "memory",
    "recall_relevant": "memory",
    "forget": "memory",
    # delegation / subagents
    "delegate_to": "delegation",
    "execute_skill": "delegation",
    # skills / playbook
    "execute_skill": "skills",
    # misc
    "subscribe_to_expediente": "judicial",
    "list_judicial_notifications": "judicial",
    "poll_judicial_now": "judicial",
    "parse_legal_email": "email",
    "sync_email_now": "email",
    "daily_briefing": "briefing",
    "run_sla_reminders": "sla",
    "sync_calendar": "calendar",
    "send_whatsapp": "messaging",
    "send_for_signature": "signatures",
    "check_signature_status": "signatures",
    "import_csv": "imports",
    "capture_lead": "crm",
    "generate_insights": "insights",
    "run_automation": "automation",
    "list_intake_forms": "intake",
    "list_new_submissions": "intake",
    "show_activity": "collaboration",
    "show_active_users": "collaboration",
}


def _infer_category(name: str) -> str:
    """Return a category string for a tool name."""
    if name in _NAME_TO_CATEGORY:
        return _NAME_TO_CATEGORY[name]
    # Prefix-based fallback
    if name.startswith("calc_"):
        return "calculations"
    if name.startswith("canvas_"):
        return "canvas"
    if name.startswith("ui_"):
        return "ui"
    if name.startswith("admin_") or name.startswith("saas_"):
        return "admin"
    return "other"


def _build_tool_list() -> list[dict]:
    """Build the tools catalog from the live registry + descriptors."""
    try:
        from api.voice import _tool_registry, _tool_descriptors
    except Exception as exc:  # noqa: BLE001
        logger.warning("voice_tools: could not import voice registry: %s", exc)
        return []

    registry_names: set[str] = set(_tool_registry.keys())
    if not registry_names:
        logger.warning("voice_tools: _tool_registry is empty (startup not complete?)")

    # Build a lookup from the explicit descriptors
    descriptors: list[dict] = _tool_descriptors()
    desc_by_name: dict[str, str] = {}
    for d in descriptors:
        name = d.get("name")
        description = d.get("description", "")
        if name and description:
            desc_by_name[name] = description

    tools: list[dict] = []
    for name in sorted(registry_names):
        description = desc_by_name.get(name) or "(sin descripcion)"
        tools.append(
            {
                "name": name,
                "description": description,
                "category": _infer_category(name),
                "voice_only": False,  # all tools are available in chat too
            }
        )

    return tools


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get("/tools")
async def list_voice_tools(
    principal: Principal = Depends(get_current_firm),
):
    """Return the full catalog of registered voice/agent tools.

    Used by CommandPaletteV2 to show all tools as executable commands.
    Reads from the in-memory registry — no DB round-trip needed.
    """
    tools = _build_tool_list()
    return {"tools": tools, "total": len(tools)}
