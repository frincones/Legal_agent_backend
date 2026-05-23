"""Agent RAG Template - Main entry point."""

from __future__ import annotations

import logging
import os
import uvicorn

from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Load environment variables from .env file
load_dotenv()

from config.schema import load_config
from utils.db import get_storage, close_storage
from utils.logger import setup_logger

logger = setup_logger("agent-rag", level=os.getenv("LOG_LEVEL", "INFO"))


async def _prewarm_openai():
    """Pre-warm the OpenAI HTTPS connection so the first user request
    doesn't pay the cold-start TLS handshake (~3s on first call).

    We send a tiny embedding request that establishes the keep-alive
    connection in the shared httpx pool. Subsequent requests reuse it.
    """
    try:
        from utils.llm import get_openai_client
        client = get_openai_client()
        # Tiny embedding to warm DNS + TLS + auth
        await client.embeddings.create(
            model="text-embedding-3-small",
            input="warmup",
        )
        logger.info("OpenAI connection pre-warmed")
    except Exception as e:
        logger.warning("OpenAI pre-warm failed (non-fatal): %s", e)


async def _prewarm_reranker():
    """Pre-load the cross-encoder model so the first re-ranking call
    doesn't pay the model download/load cost (~25s on first ever call,
    ~2s on subsequent cold loads after restart).
    """
    try:
        import asyncio
        from retrieval.reranker import _get_reranker

        # Run the synchronous model load in a thread to not block startup
        def _load():
            _get_reranker("cross-encoder/ms-marco-MiniLM-L-6-v2")

        await asyncio.to_thread(_load)
        logger.info("Cross-encoder re-ranker pre-loaded")
    except Exception as e:
        logger.warning("Re-ranker pre-warm failed (non-fatal): %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    config = load_config()
    logger.info("Starting Agent RAG Template: %s (%s)", config.agent.name, config.agent.role)

    # Initialize storage on startup
    storage = await get_storage(config.storage)
    logger.info("Storage initialized: %s", config.storage.provider)

    # Sprint L-DOC: auto-apply migrations idempotente
    try:
        from storage.auto_migrate import run_sprint_l_doc_migrations
        await run_sprint_l_doc_migrations(storage.pool)
    except Exception as e:
        logger.warning("Sprint L-DOC auto_migrate failed (non-fatal): %s", e)

    # Pre-warm OpenAI connection to eliminate cold-start latency on first request
    await _prewarm_openai()

    # Pre-load the cross-encoder reranker model (skip if disabled or via env)
    if config.retrieval.reranking.enabled and os.getenv("SKIP_RERANKER_PREWARM") != "1":
        await _prewarm_reranker()
    else:
        logger.info("Re-ranker prewarm skipped (disabled in config or env)")

    # Initialize legal sources and derogation graph
    try:
        from legal_sources.source_router import LegalSourceRouter
        from derogation.graph import DerogationGraph
        from derogation.vigencia_checker import VigenciaChecker
        from ingestion.embedder import create_embedder
        from api.legal import init_legal_api

        legal_config = getattr(config, 'legal_sources', None)
        legal_config_dict = legal_config.model_dump() if legal_config else {}
        source_router = LegalSourceRouter(legal_config_dict)

        derogation_graph = DerogationGraph(storage.pool)
        vigencia_checker = VigenciaChecker(derogation_graph)
        embedder = create_embedder(
            model=config.ingestion.embedding.model,
            use_cache=True,
        )

        init_legal_api(source_router, derogation_graph, vigencia_checker, storage, embedder)
        logger.info("Legal sources and derogation graph initialized")
    except Exception as e:
        logger.warning("Legal system init failed (non-fatal): %s", e)

    # LexAI · Register Realtime tool implementations
    try:
        from api.voice import register_tool
        from api.citations import (
            research_jurisprudence_tool,
            validate_citation_tool,
            validate_norm_vigencia_tool,
        )
        from api.calc import calc_liquidacion_tool, calc_prescripcion_tool, calc_intereses_tool
        from api.matters import open_matter_context_tool
        from api.hitl import request_approval_tool
        from agent.tools.draft_pleading import draft_pleading_tool, list_legal_templates_tool
        from agent.tools.paralegal_tools import (
            list_my_matters_tool,
            find_client_tool,
            list_upcoming_deadlines_tool,
            add_matter_deadline_tool,
            mark_deadline_done_tool,
            add_matter_note_tool,
            list_matter_documents_tool,
            summarize_document_tool,
            list_pending_hitl_tool,
            get_firm_metrics_tool,
        )

        # Core 7
        register_tool("research_jurisprudence", research_jurisprudence_tool)
        register_tool("validate_citation", validate_citation_tool)
        register_tool("validate_norm_vigencia", validate_norm_vigencia_tool)
        register_tool("calc_liquidacion", calc_liquidacion_tool)
        register_tool("open_matter_context", open_matter_context_tool)
        register_tool("request_human_approval", request_approval_tool)
        register_tool("draft_pleading", draft_pleading_tool)
        register_tool("list_legal_templates", list_legal_templates_tool)
        # Paralegal-grade extensions (10)
        register_tool("list_my_matters", list_my_matters_tool)
        register_tool("find_client", find_client_tool)
        register_tool("list_upcoming_deadlines", list_upcoming_deadlines_tool)
        register_tool("add_matter_deadline", add_matter_deadline_tool)
        register_tool("mark_deadline_done", mark_deadline_done_tool)
        register_tool("add_matter_note", add_matter_note_tool)
        register_tool("list_matter_documents", list_matter_documents_tool)
        register_tool("summarize_document", summarize_document_tool)
        register_tool("list_pending_hitl", list_pending_hitl_tool)
        register_tool("get_firm_metrics", get_firm_metrics_tool)
        # F3 calculadoras
        register_tool("calc_prescripcion", calc_prescripcion_tool)
        register_tool("calc_intereses", calc_intereses_tool)
        # F1 análisis de documentos
        from agent.tools.document_analysis import extract_document_entities_tool
        register_tool("extract_document_entities", extract_document_entities_tool)
        # F2 judicial notifications
        from api.notifications import (
            subscribe_to_expediente_tool,
            list_judicial_notifications_tool,
            poll_judicial_now_tool,
        )
        register_tool("subscribe_to_expediente", subscribe_to_expediente_tool)
        register_tool("list_judicial_notifications", list_judicial_notifications_tool)
        register_tool("poll_judicial_now", poll_judicial_now_tool)
        # Sprint 5 · email parser + daily briefing + SLA
        from agent.tools.parse_legal_email import parse_legal_email_tool
        from agent.tools.daily_briefing import daily_briefing_tool
        from agent.workers.sla_reminders import run_sla_reminders_tool
        register_tool("parse_legal_email", parse_legal_email_tool)
        register_tool("daily_briefing", daily_briefing_tool)
        register_tool("run_sla_reminders", run_sla_reminders_tool)
        # Sprint 6 · billing/audit/quotas + email sync
        from agent.workers.email_ingest import sync_email_now_tool
        from api.audit import query_audit_logs_tool
        from api.quotas import check_quota_tool
        register_tool("sync_email_now", sync_email_now_tool)
        register_tool("query_audit_logs", query_audit_logs_tool)
        register_tool("check_quota", check_quota_tool)
        # Sprint 7 · calendar + analytics + search + whatsapp
        from agent.workers.calendar_sync import sync_calendar_tool
        from api.analytics import analyze_firm_performance_tool
        from api.search import find_anything_tool
        from api.whatsapp import send_whatsapp_tool
        register_tool("sync_calendar", sync_calendar_tool)
        register_tool("analyze_firm_performance", analyze_firm_performance_tool)
        register_tool("find_anything", find_anything_tool)
        register_tool("send_whatsapp", send_whatsapp_tool)
        # Sprint 8 · billable hours + invoicing
        from api.time_entries import track_time_tool
        from api.expenses import log_expense_tool
        from api.invoices import generate_invoice_tool
        register_tool("track_time", track_time_tool)
        register_tool("log_expense", log_expense_tool)
        register_tool("generate_invoice", generate_invoice_tool)
        # Sprint 9 · CRM + insights + automation
        from api.leads import capture_lead_tool
        from api.ai_insights import generate_insights_tool
        from api.automation import run_automation_tool
        register_tool("capture_lead", capture_lead_tool)
        register_tool("generate_insights", generate_insights_tool)
        register_tool("run_automation", run_automation_tool)
        # Sprint 10 · Trust accounting
        from api.trust_accounts import check_trust_balance_tool
        from api.trust_transactions import record_trust_deposit_tool, record_trust_payment_tool
        register_tool("check_trust_balance", check_trust_balance_tool)
        register_tool("record_trust_deposit", record_trust_deposit_tool)
        register_tool("record_trust_payment", record_trust_payment_tool)
        # Sprint 11 · Document AI v2
        from api.contract_analyzer import analyze_contract_voice_tool
        from api.doc_qa import ask_about_document_tool
        from api.doc_compare import compare_documents_tool
        register_tool("analyze_contract", analyze_contract_voice_tool)
        register_tool("ask_about_document", ask_about_document_tool)
        register_tool("compare_documents", compare_documents_tool)
        # Sprint 13 · Signatures + Imports
        from api.signatures import send_for_signature_tool, check_signature_status_tool
        from api.imports import import_csv_tool
        register_tool("send_for_signature", send_for_signature_tool)
        register_tool("check_signature_status", check_signature_status_tool)
        register_tool("import_csv", import_csv_tool)
        # Sprint 15 · Knowledge Base + Memoria del despacho
        from agent.tools.kb_voice import (
            search_kb_voice_tool, add_to_kb_voice_tool,
            search_lessons_voice_tool,
        )
        from agent.tools.extract_lessons import extract_lesson_tool
        register_tool("search_kb", search_kb_voice_tool)
        register_tool("add_to_kb", add_to_kb_voice_tool)
        register_tool("search_lessons", search_lessons_voice_tool)
        register_tool("extract_lesson", extract_lesson_tool)
        # Sprint 16 · Colaboración
        from agent.tools.collab import (
            add_comment_tool, resolve_comment_tool,
            show_activity_tool, show_active_users_tool,
        )
        register_tool("add_comment", add_comment_tool)
        register_tool("resolve_comment", resolve_comment_tool)
        register_tool("show_activity", show_activity_tool)
        register_tool("show_active_users", show_active_users_tool)
        # Sprint 17 · Predicciones + Tasks + My Day
        from agent.tools.predict_outcome import predict_outcome_tool
        from agent.tools.productivity import (
            create_task_tool, complete_task_tool,
            what_today_tool, what_is_my_priority_tool,
        )
        register_tool("predict_outcome", predict_outcome_tool)
        register_tool("create_task", create_task_tool)
        register_tool("complete_task", complete_task_tool)
        register_tool("what_today", what_today_tool)
        register_tool("what_is_my_priority", what_is_my_priority_tool)
        # Sprint M · Matter management (5 tools) · cubren huecos detectados
        # en la auditoría agent-ui-sync: set_priority, tag, etapa, archive,
        # create_matter. Todos emiten data_changed con resource='matters'.
        from agent.tools.matter_management import (
            set_matter_priority_tool, tag_matter_tool,
            update_matter_etapa_tool, archive_matter_tool, create_matter_tool,
        )
        register_tool("set_matter_priority", set_matter_priority_tool)
        register_tool("tag_matter", tag_matter_tool)
        register_tool("update_matter_etapa", update_matter_etapa_tool)
        register_tool("archive_matter", archive_matter_tool)
        register_tool("create_matter", create_matter_tool)
        # Sprint 18 · Analytics ejecutivos
        from agent.tools.analytics_voice import (
            firm_revenue_tool, lawyer_performance_tool,
            prediction_accuracy_tool, executive_kpis_tool,
        )
        register_tool("firm_revenue", firm_revenue_tool)
        register_tool("lawyer_performance", lawyer_performance_tool)
        register_tool("prediction_accuracy", prediction_accuracy_tool)
        register_tool("executive_kpis", executive_kpis_tool)
        # Sprint 19 · Intake + Smart Doc Fill
        from agent.tools.doc_smart_fill import (
            autofill_template_tool, extract_variables_from_text_tool,
            list_intake_forms_tool, list_new_submissions_tool,
        )
        register_tool("autofill_template", autofill_template_tool)
        register_tool("extract_variables_from_text", extract_variables_from_text_tool)
        register_tool("list_intake_forms", list_intake_forms_tool)
        register_tool("list_new_submissions", list_new_submissions_tool)
        # Sprint 20 · Judge Perspective Simulator (M25)
        from agent.tools.judge_voice import (
            search_judge_tool, get_judge_stats_tool,
        )
        from agent.tools.judge_simulator import simulate_judge_view_tool
        register_tool("search_judge", search_judge_tool)
        register_tool("get_judge_stats", get_judge_stats_tool)
        register_tool("simulate_judge_view", simulate_judge_view_tool)
        # Sprint 21 · Evidence Authenticity Checker (M29)
        from agent.tools.evidence_voice import (
            validate_identity_tool, check_doc_consistency_tool, score_evidence_tool,
        )
        register_tool("validate_identity", validate_identity_tool)
        register_tool("check_doc_consistency", check_doc_consistency_tool)
        register_tool("score_evidence", score_evidence_tool)
        # Sprint 22 · Client Portal B2C wizards (M34)
        from agent.tools.wizard_voice import (
            list_wizards_tool, start_wizard_tool, wizard_session_status_tool,
        )
        register_tool("list_wizards", list_wizards_tool)
        register_tool("start_wizard", start_wizard_tool)
        register_tool("wizard_session_status", wizard_session_status_tool)
        # Sprint E · Skills + Redlines voice tools
        from agent.tools.skills_voice import (
            execute_skill_tool, review_contract_tool,
            apply_redline_tool, reject_redline_tool,
        )
        register_tool("execute_skill", execute_skill_tool)
        register_tool("review_contract", review_contract_tool)
        register_tool("apply_redline", apply_redline_tool)
        register_tool("reject_redline", reject_redline_tool)
        # Sprint 23 · Billing voice tools (M36)
        from agent.tools.billing_voice import (
            current_plan_status_tool, remaining_quota_tool, pricing_recommendation_tool,
        )
        register_tool("current_plan_status", current_plan_status_tool)
        register_tool("remaining_quota", remaining_quota_tool)
        register_tool("pricing_recommendation", pricing_recommendation_tool)
        # Sprint 24 · SaaS Admin voice tools
        from agent.tools.admin_voice import (
            saas_mrr_now_tool, saas_signups_mtd_tool, saas_churn_30d_tool,
            search_firm_by_name_tool, firm_health_snapshot_tool,
        )
        register_tool("saas_mrr_now", saas_mrr_now_tool)
        register_tool("saas_signups_mtd", saas_signups_mtd_tool)
        register_tool("saas_churn_30d", saas_churn_30d_tool)
        register_tool("search_firm_by_name", search_firm_by_name_tool)
        register_tool("firm_health_snapshot", firm_health_snapshot_tool)
        # F1 v2 · UI bridge
        from agent.tools.ui_bridge import (
            ui_navigate_tool, ui_open_matter_canvas_tool, ui_open_matter_tab_tool,
            ui_scroll_to_tool, ui_open_command_palette_tool, ui_prefill_form_tool,
            ui_show_toast_tool, ui_open_modal_tool,
        )
        register_tool("ui_navigate", ui_navigate_tool)
        register_tool("ui_open_matter_canvas", ui_open_matter_canvas_tool)
        register_tool("ui_open_matter_tab", ui_open_matter_tab_tool)
        register_tool("ui_scroll_to", ui_scroll_to_tool)
        register_tool("ui_open_command_palette", ui_open_command_palette_tool)
        register_tool("ui_prefill_form", ui_prefill_form_tool)
        register_tool("ui_show_toast", ui_show_toast_tool)
        register_tool("ui_open_modal", ui_open_modal_tool)
        # F2 · External research (httpx-only)
        from agent.tools.external_research import (
            search_suin_juriscol_tool, verify_rue_persona_tool,
            fetch_dof_co_publicacion_tool, fetch_banrep_dtf_tool,
        )
        register_tool("search_suin_juriscol", search_suin_juriscol_tool)
        register_tool("verify_rue_persona", verify_rue_persona_tool)
        register_tool("fetch_dof_co_publicacion", fetch_dof_co_publicacion_tool)
        register_tool("fetch_banrep_dtf", fetch_banrep_dtf_tool)
        # F3 · Subagent delegation (aditivo, no remueve tools existentes)
        from agent.subagents.registry import delegate_to_tool
        register_tool("delegate_to", delegate_to_tool)
        # F5 · Memoria persistente
        from agent.tools.memory import (
            remember_tool, recall_tool, recall_relevant_tool, forget_tool,
        )
        register_tool("remember", remember_tool)
        register_tool("recall", recall_tool)
        register_tool("recall_relevant", recall_relevant_tool)
        register_tool("forget", forget_tool)
        # F-Canvas · co-edicion del documento (9 tools · v1 + v2 ProseMirror-native)
        from agent.tools.canvas_edit import (
            canvas_set_text_tool, canvas_append_tool,
            canvas_replace_section_tool, canvas_save_version_tool,
            canvas_get_current_tool, get_document_content_tool,
            canvas_insert_at_cursor_tool, canvas_find_replace_tool,
            canvas_select_section_tool,
        )
        register_tool("canvas_set_text", canvas_set_text_tool)
        register_tool("canvas_append", canvas_append_tool)
        register_tool("canvas_replace_section", canvas_replace_section_tool)
        register_tool("canvas_save_version", canvas_save_version_tool)
        register_tool("canvas_get_current", canvas_get_current_tool)
        register_tool("get_document_content", get_document_content_tool)
        register_tool("canvas_insert_at_cursor", canvas_insert_at_cursor_tool)
        register_tool("canvas_find_replace", canvas_find_replace_tool)
        register_tool("canvas_select_section", canvas_select_section_tool)
        logger.info("LexAI Realtime tools registered (50 total · 3 sub-agentes · memoria · canvas v2)")
    except Exception as e:
        logger.warning("LexAI tool registration failed (non-fatal): %s", e)

    yield

    # Cleanup on shutdown
    await close_storage()
    logger.info("Shutdown complete")


app = FastAPI(
    title="Agent RAG Template",
    description="Customizable RAG agent with Level 3 ingestion and Level 5 retrieval",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Sprint 28 · Security headers middleware
from starlette.middleware.base import BaseHTTPMiddleware


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds standard security headers to all responses."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(self), geolocation=()")
        # HSTS only when https (Railway terminates TLS)
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response


app.add_middleware(SecurityHeadersMiddleware)

# Register routers
from api.health import router as health_router
from api.chat import router as chat_router
from api.ingest import router as ingest_router
from api.documents import router as documents_router
from api.sessions import router as sessions_router
from api.usage import router as usage_router
from api.legal import router as legal_router

# LexAI MVP routers (multi-tenant, JWT-protected)
from api.voice import router as voice_router
from api.calc import router as calc_router
from api.citations import router as citations_router
from api.catastro_conciliacion import router as catastro_conciliacion_router
from api.hitl import router as hitl_router
from api.matters import router as matters_router
from api.clients import router as clients_router
from api.matter_documents import router as matter_documents_router
from api.notifications import router as notifications_router
from api.legal_templates_api import router as legal_templates_router
from api.canvas_transform import router as canvas_transform_router
from api.canvas_generate import router as canvas_generate_router
from api.canvas_collab import router as canvas_collab_router
from api.firm_teams import router as firm_teams_router
from api.firm_users import router as firm_users_router
from api.profile import router as profile_router
from api.legal_alerts import router as legal_alerts_router
from api.user_templates import router as user_templates_router
from api.calc_plazos import router as calc_plazos_router
from api.calc_pension import router as calc_pension_router
from api.judicial import router as judicial_router
from api.email_integrations import router as email_integrations_router
from api.integrations import router as integrations_router  # sprint A · unified OAuth registry
from api.calendar_events_create import router as calendar_events_create_router  # sprint B · POST /v1/calendar/events
from api.admin_sync_tick import router as admin_sync_tick_router  # sprint B/C · pg_cron tick
from api.cloud_documents import router as cloud_documents_router  # sprint C · Drive/OneDrive/Dropbox
from api.docusign import router as docusign_router  # sprint D · DocuSign envelopes
# Sprint E · Drafting + Review (igualar Claude for Legal)
from api.skills import router as skills_router
from api.playbook import router as playbook_router
from api.canvas_redlines import router as canvas_redlines_router
from api.canvas_export import router as canvas_export_router
from api.saas_skills_admin import router as saas_skills_admin_router
# Templates · system catalog search + multi-agent generator (Sprints 1-3 templates)
from api.templates_search import router as templates_search_router
from api.multi_agent_generate import router as multi_agent_generate_router
# Admin · per-tool integration test endpoint (used by scripts/test_all_tools_remote.py)
from api.admin_tools_test import router as admin_tools_test_router
from api.inbox import router as inbox_router
from api.push import router as push_router
from api.sla import router as sla_router
from fastapi import Depends as _Depends_S25  # for Sprint 25 entitlements
from utils.entitlements import requires_module as _req_mod_S25
from api.billing import router as billing_router
from api.billing_admin import router as billing_admin_router
from api.admin_tenants import router as admin_tenants_router
from api.admin_users import router as admin_users_router
from api.admin_feature_flags import router as admin_flags_router
from api.admin_cartera import router as admin_cartera_router
from api.admin_metrics import router as admin_metrics_router
from api.admin_support import router_admin as admin_support_router, router_client as support_client_router
from api.admin_impersonate import router as admin_impersonate_router
from api.admin_entitlements import router as admin_entitlements_router
from api.entitlements_client import router as entitlements_client_router
from api.onboarding import router as onboarding_router
from api.admin_helper import router as admin_helper_router, welcome_router as admin_welcome_router
from api.public_landing import router as public_landing_router
from api.admin_landing import changelog_router as admin_changelog_router, testimonials_router as admin_testimonials_router
from api.arco_requests import (
    client_router as arco_client_router,
    public_router as arco_public_router,
    admin_router as arco_admin_router,
)
from api.status_public import public_router as status_public_router, admin_router as status_admin_router
from api.firm_invites import router as firm_invites_router
from api.audit import router as audit_router
from api.quotas import router as quotas_router
from api.calendar_integrations import router as calendar_router
from api.analytics import router as analytics_router
from api.search import router as search_router
from api.whatsapp import router as whatsapp_router
from api.time_entries import router as time_entries_router
from api.expenses import router as expenses_router
from api.invoices import router as invoices_router
from api.client_portal import router_admin as client_portal_admin_router, router_public as client_portal_public_router
from api.leads import router as leads_router
from api.lead_stages import router as lead_stages_router
from api.ai_insights import router as ai_insights_router
from api.automation import router as automation_router
from api.trust_accounts import router as trust_accounts_router
from api.trust_transactions import router as trust_transactions_router
from api.bank_reconciliation import router as bank_reconciliation_router
from api.contract_analyzer import router as contract_analyzer_router
from api.doc_qa import router as doc_qa_router
from api.doc_compare import router as doc_compare_router
from api.sync import router as sync_router
from api.signatures import router as signatures_router
from api.imports import router as imports_router
from api.api_keys import router as api_keys_router
from api.public import router as public_router
from api.webhooks_outbound import router as webhooks_outbound_router
from api.marketplace import router as marketplace_router
from api.knowledge_base import router as kb_router
from api.case_lessons import router as case_lessons_router
from api.kb_admin import router as kb_admin_router
from api.comments import router as comments_router
from api.presence import router as presence_router
from api.activity import router as activity_router
from api.mentions import router as mentions_router
from api.predictions import router as predictions_router
from api.tasks import router as tasks_router
from api.my_day import router as my_day_router
from api.saved_filters import router as saved_filters_router
from api.analytics_v2 import router as analytics_v2_router
from api.reports import router as reports_router
from api.analytics_admin import router as analytics_admin_router
from api.intake_forms import router as intake_forms_router, subm_router as intake_submissions_router
from api.intake_public import router as intake_public_router
from api.template_intelligence import router as template_ai_router
from api.judges import router as judges_router
from api.judge_simulator import router as judge_predictions_router
from api.judges_admin import router as judges_admin_router
from api.evidence import router as evidence_router
from api.wizard_templates import router as wizard_templates_router, sessions_router as wizard_sessions_router
from api.wizard_public import router as wizard_public_router
# F1 UX v2 · complementary endpoints for sidebar + command palette
from api.voice_tools import router as voice_tools_router
from api.threads import router as threads_router

app.include_router(health_router, prefix="/api")
app.include_router(chat_router, prefix="/api")
app.include_router(ingest_router, prefix="/api")
app.include_router(documents_router, prefix="/api")
app.include_router(sessions_router, prefix="/api")
app.include_router(usage_router, prefix="/api")
app.include_router(legal_router)  # Already has /api/legal prefix

# LexAI · /v1/* (these routers self-prefix with /v1/...)
app.include_router(voice_router)
app.include_router(calc_router)
app.include_router(citations_router)
app.include_router(catastro_conciliacion_router)
app.include_router(hitl_router)
app.include_router(matters_router)
app.include_router(clients_router)
app.include_router(matter_documents_router)
app.include_router(notifications_router)
app.include_router(legal_templates_router)
app.include_router(canvas_transform_router, dependencies=[_Depends_S25(_req_mod_S25("canvas"))])
app.include_router(canvas_generate_router, dependencies=[_Depends_S25(_req_mod_S25("canvas"))])
# Sprint J · WS de colaboración no se puede gateear por dependency (HTTP),
# pero el ticket HMAC valida firm_id; el endpoint /ws-ticket POST sí está gateado.
app.include_router(canvas_collab_router)
# Templates · /v1/templates/* (search + by-id)
app.include_router(templates_search_router)
# Multi-agent document generator · /v1/multi-agent/*
app.include_router(multi_agent_generate_router)
# Admin · per-tool integration test · /v1/admin/tools/*
app.include_router(admin_tools_test_router)
app.include_router(firm_teams_router)
app.include_router(firm_users_router)
app.include_router(profile_router)
app.include_router(legal_alerts_router)
app.include_router(user_templates_router)
app.include_router(calc_plazos_router)
app.include_router(calc_pension_router)
app.include_router(judicial_router)
app.include_router(email_integrations_router, dependencies=[_Depends_S25(_req_mod_S25("email_ingest"))])
# Sprint A · integraciones unificadas (google_drive, onedrive, dropbox, docusign)
# Sin entitlement gate de origen · cada feature gateada en sus routers específicos (sprints C/D)
app.include_router(integrations_router)
# Sprint B · POST /v1/calendar/events (crear audiencias desde LexAI)
app.include_router(calendar_events_create_router, dependencies=[_Depends_S25(_req_mod_S25("calendar_sync"))])
# Sprint B/C · admin sync-tick para pg_cron
app.include_router(admin_sync_tick_router)
# Sprint C · documentos cloud (drive/onedrive/dropbox)
app.include_router(cloud_documents_router)
# Sprint D · DocuSign envelopes (gateado por entitlement signatures existente sprint 25)
app.include_router(docusign_router, dependencies=[_Depends_S25(_req_mod_S25("signatures"))])
# Sprint E · Skills + Playbook + Canvas Redlines + Export
app.include_router(skills_router, dependencies=[_Depends_S25(_req_mod_S25("skill_drafting"))])
app.include_router(playbook_router)
app.include_router(canvas_redlines_router, dependencies=[_Depends_S25(_req_mod_S25("redline_studio"))])
app.include_router(canvas_export_router)
app.include_router(saas_skills_admin_router)
app.include_router(inbox_router)
app.include_router(push_router)
app.include_router(sla_router)
app.include_router(billing_router)
app.include_router(billing_admin_router)
app.include_router(admin_tenants_router)
app.include_router(admin_users_router)
app.include_router(admin_flags_router)
app.include_router(admin_cartera_router)
app.include_router(admin_metrics_router)
app.include_router(admin_support_router)
app.include_router(support_client_router)
app.include_router(admin_impersonate_router)
app.include_router(admin_entitlements_router)
app.include_router(entitlements_client_router)
app.include_router(onboarding_router)
app.include_router(admin_helper_router)
app.include_router(admin_welcome_router)
app.include_router(public_landing_router)
app.include_router(admin_changelog_router)
app.include_router(admin_testimonials_router)
app.include_router(arco_client_router)
app.include_router(arco_public_router)
app.include_router(arco_admin_router)
app.include_router(status_public_router)
app.include_router(status_admin_router)
app.include_router(firm_invites_router)
app.include_router(audit_router)
app.include_router(quotas_router)
app.include_router(calendar_router, dependencies=[_Depends_S25(_req_mod_S25("calendar_sync"))])
app.include_router(analytics_router)
app.include_router(search_router)
app.include_router(whatsapp_router, dependencies=[_Depends_S25(_req_mod_S25("whatsapp_integration"))])
app.include_router(time_entries_router)
app.include_router(expenses_router)
app.include_router(invoices_router)
app.include_router(client_portal_admin_router, dependencies=[_Depends_S25(_req_mod_S25("client_portal"))])
app.include_router(client_portal_public_router)  # public · sin auth
app.include_router(leads_router, dependencies=[_Depends_S25(_req_mod_S25("leads_crm"))])
app.include_router(lead_stages_router, dependencies=[_Depends_S25(_req_mod_S25("leads_crm"))])
app.include_router(ai_insights_router, dependencies=[_Depends_S25(_req_mod_S25("ai_insights"))])
app.include_router(automation_router, dependencies=[_Depends_S25(_req_mod_S25("automation_rules"))])
app.include_router(trust_accounts_router, dependencies=[_Depends_S25(_req_mod_S25("trust_accounts"))])
app.include_router(trust_transactions_router, dependencies=[_Depends_S25(_req_mod_S25("trust_accounts"))])
app.include_router(bank_reconciliation_router, dependencies=[_Depends_S25(_req_mod_S25("trust_accounts"))])
app.include_router(contract_analyzer_router, dependencies=[_Depends_S25(_req_mod_S25("contract_analyzer"))])
app.include_router(doc_qa_router, dependencies=[_Depends_S25(_req_mod_S25("doc_qa"))])
app.include_router(doc_compare_router, dependencies=[_Depends_S25(_req_mod_S25("doc_compare"))])
app.include_router(sync_router)
app.include_router(signatures_router, dependencies=[_Depends_S25(_req_mod_S25("signatures"))])
app.include_router(imports_router)
app.include_router(api_keys_router, dependencies=[_Depends_S25(_req_mod_S25("api_public"))])
app.include_router(public_router)  # consumido con API key, su propio gate
app.include_router(webhooks_outbound_router, dependencies=[_Depends_S25(_req_mod_S25("webhooks_outbound"))])
app.include_router(marketplace_router, dependencies=[_Depends_S25(_req_mod_S25("marketplace"))])
app.include_router(kb_router, dependencies=[_Depends_S25(_req_mod_S25("knowledge_base"))])
app.include_router(case_lessons_router, dependencies=[_Depends_S25(_req_mod_S25("lessons"))])
app.include_router(kb_admin_router, dependencies=[_Depends_S25(_req_mod_S25("knowledge_base"))])
app.include_router(comments_router)
app.include_router(presence_router)
app.include_router(activity_router)
app.include_router(mentions_router)
app.include_router(predictions_router, dependencies=[_Depends_S25(_req_mod_S25("predictions"))])
app.include_router(tasks_router)
app.include_router(my_day_router)
app.include_router(saved_filters_router)
app.include_router(analytics_v2_router, dependencies=[_Depends_S25(_req_mod_S25("analytics_executive"))])
app.include_router(reports_router, dependencies=[_Depends_S25(_req_mod_S25("reports_custom"))])
app.include_router(analytics_admin_router)
app.include_router(intake_forms_router, dependencies=[_Depends_S25(_req_mod_S25("intake_forms"))])
app.include_router(intake_submissions_router, dependencies=[_Depends_S25(_req_mod_S25("intake_forms"))])
app.include_router(intake_public_router)  # public · no gate (sin auth)
app.include_router(template_ai_router)
app.include_router(judges_router, dependencies=[_Depends_S25(_req_mod_S25("judges"))])
app.include_router(judge_predictions_router, dependencies=[_Depends_S25(_req_mod_S25("judge_simulator"))])
app.include_router(judges_admin_router, dependencies=[_Depends_S25(_req_mod_S25("judges"))])
app.include_router(evidence_router, dependencies=[_Depends_S25(_req_mod_S25("evidence_checker"))])
app.include_router(wizard_templates_router, dependencies=[_Depends_S25(_req_mod_S25("wizards_public"))])
app.include_router(wizard_sessions_router, dependencies=[_Depends_S25(_req_mod_S25("wizards_public"))])
app.include_router(wizard_public_router)  # public · sin gate
# F1 UX v2 · GET /v1/voice/tools (CommandPaletteV2) + GET /v1/threads (SidebarHilosList)
app.include_router(voice_tools_router)
app.include_router(threads_router)

# Sprint L-DOC · Admin Pipeline endpoints (/admin/pipeline/*)
try:
    from api.admin_pipeline import router as admin_pipeline_router
    app.include_router(admin_pipeline_router)
    logger.info("admin_pipeline router registered")
except Exception as _e:
    logger.warning("admin_pipeline router registration failed: %s", _e)

# Sprint L-DOC · Test endpoints para validar 17 fuentes
try:
    from api.admin_pipeline_test import router as admin_pipeline_test_router
    app.include_router(admin_pipeline_test_router)
    logger.info("admin_pipeline_test router registered")
except Exception as _e:
    logger.warning("admin_pipeline_test router registration failed: %s", _e)

# Sprint L-DOC · Ingest URL endpoint (validar fuentes con 1 doc real)
try:
    from api.admin_ingest_url import router as admin_ingest_url_router
    app.include_router(admin_ingest_url_router)
    logger.info("admin_ingest_url router registered")
except Exception as _e:
    logger.warning("admin_ingest_url router registration failed: %s", _e)

# Sprint L-DOC · POST /v1/documents/generate (SSE v1)
try:
    from api.documents_generate import router as documents_generate_router
    app.include_router(documents_generate_router)
    logger.info("documents_generate router registered")
except Exception as _e:
    logger.warning("documents_generate router registration failed: %s", _e)

# Sprint M · POST /v1/documents/v2/generate (block-level SSE v2)
# Feature flag FLAG_DOCGEN_V2 controla disponibilidad (default OFF en prod hasta validar)
try:
    from api.documents_generate_v2 import router as documents_generate_v2_router
    app.include_router(documents_generate_v2_router)
    logger.info("documents_generate_v2 router registered (flag check at request time)")
except Exception as _e:
    logger.warning("documents_generate_v2 router registration failed: %s", _e)


def main():
    """Run the application."""
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))

    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=os.getenv("ENV", "development") == "development",
    )


if __name__ == "__main__":
    main()
