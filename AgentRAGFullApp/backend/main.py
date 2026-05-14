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
from api.hitl import router as hitl_router
from api.matters import router as matters_router
from api.clients import router as clients_router
from api.matter_documents import router as matter_documents_router
from api.notifications import router as notifications_router
from api.legal_templates_api import router as legal_templates_router
from api.canvas_transform import router as canvas_transform_router
from api.canvas_generate import router as canvas_generate_router
from api.firm_teams import router as firm_teams_router
from api.firm_users import router as firm_users_router
from api.profile import router as profile_router
from api.legal_alerts import router as legal_alerts_router
from api.user_templates import router as user_templates_router
from api.calc_plazos import router as calc_plazos_router
from api.calc_pension import router as calc_pension_router
from api.judicial import router as judicial_router
from api.email_integrations import router as email_integrations_router
from api.inbox import router as inbox_router
from api.push import router as push_router
from api.sla import router as sla_router
from api.billing import router as billing_router
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
app.include_router(hitl_router)
app.include_router(matters_router)
app.include_router(clients_router)
app.include_router(matter_documents_router)
app.include_router(notifications_router)
app.include_router(legal_templates_router)
app.include_router(canvas_transform_router)
app.include_router(canvas_generate_router)
app.include_router(firm_teams_router)
app.include_router(firm_users_router)
app.include_router(profile_router)
app.include_router(legal_alerts_router)
app.include_router(user_templates_router)
app.include_router(calc_plazos_router)
app.include_router(calc_pension_router)
app.include_router(judicial_router)
app.include_router(email_integrations_router)
app.include_router(inbox_router)
app.include_router(push_router)
app.include_router(sla_router)
app.include_router(billing_router)
app.include_router(audit_router)
app.include_router(quotas_router)
app.include_router(calendar_router)
app.include_router(analytics_router)
app.include_router(search_router)
app.include_router(whatsapp_router)
app.include_router(time_entries_router)
app.include_router(expenses_router)
app.include_router(invoices_router)
app.include_router(client_portal_admin_router)
app.include_router(client_portal_public_router)
app.include_router(leads_router)
app.include_router(lead_stages_router)
app.include_router(ai_insights_router)
app.include_router(automation_router)
app.include_router(trust_accounts_router)
app.include_router(trust_transactions_router)
app.include_router(bank_reconciliation_router)
app.include_router(contract_analyzer_router)
app.include_router(doc_qa_router)
app.include_router(doc_compare_router)
app.include_router(sync_router)
app.include_router(signatures_router)
app.include_router(imports_router)
app.include_router(api_keys_router)
app.include_router(public_router)
app.include_router(webhooks_outbound_router)
app.include_router(marketplace_router)


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
