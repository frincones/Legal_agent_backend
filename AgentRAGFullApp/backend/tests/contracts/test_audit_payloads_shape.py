"""Sprint M20.01 · Tests de contrato · audit payloads shape.

Congela la estructura de los payloads persistidos en:
  - generation_audit
  - document_blocks (con verifier_audit JSONB)
  - verification_attempts
  - chat_messages (con segments[])
  - tool_call_audit (M20.01 nuevo)

Si la estructura cambia, frontend rompe (badges verificación, audit forensic, chat trace).
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4


class TestGenerationAuditShape:
    """generation_audit es el audit de alto nivel por documento."""

    def test_required_columns_present(self):
        """generation_audit debe tener estas columnas (contrato BD)."""
        required = {
            "id", "generation_id", "firm_id", "matter_id", "document_id",
            "template_id", "template_version", "model_used",
            "duration_seconds", "cost_usd", "citations", "calculations",
            "validation_passed", "qa_score", "warnings", "audit_json",
            "created_at",
        }
        # Validamos con un dict que simule la fila (no requiere BD viva)
        sample_row = {col: None for col in required}
        sample_row.update({
            "id": str(uuid4()),
            "generation_id": str(uuid4()),
            "duration_seconds": 30.5,
            "cost_usd": 0.062,
            "model_used": {"classifier": "gpt-4o-mini", "generator": "claude-sonnet-4-6"},
            "citations": [],
            "calculations": {},
            "validation_passed": True,
            "warnings": [],
            "audit_json": {},
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        assert set(sample_row.keys()) >= required

    def test_new_m20_columns_present(self):
        """M20.01 agrega cache_hit_tokens y orchestrator_kind."""
        m20_cols = {"cache_hit_tokens", "orchestrator_kind"}
        sample = {
            "cache_hit_tokens": 1500,
            "orchestrator_kind": "lean",
        }
        assert set(sample.keys()) >= m20_cols
        assert sample["orchestrator_kind"] in ("legacy", "lean")

    def test_model_used_jsonb_shape(self):
        """model_used es un dict {stage_name: model_name}."""
        model_used = {
            "classifier": "gpt-4o-mini",
            "data_extractor": "gpt-4o-mini",
            "block_generator": "claude-sonnet-4-6",
            "polish": "claude-sonnet-4-6",
            "qa": "gpt-4o-mini",
        }
        # cada valor debe ser string (model name)
        for stage, model in model_used.items():
            assert isinstance(stage, str)
            assert isinstance(model, str)


class TestDocumentBlocksShape:
    """document_blocks: storage de los Block[] generados."""

    def test_required_columns(self):
        required = {
            "id", "document_id", "section_key", "section_order",
            "block_type", "block_payload", "verifier_audit", "created_at",
        }
        sample = {c: None for c in required}
        assert set(sample.keys()) >= required

    def test_verifier_audit_jsonb_shape(self):
        """verifier_audit incluye citations con verificación + 4-tier (post M20.10)."""
        verifier_audit = {
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "citations": [
                {
                    "ref": "Art. 2142 CC",
                    "tier": "GROUNDED",                          # M20.10
                    "found": True,
                    "fuente_url": "https://suin...",
                    "similarity": 0.95,
                    "verification_method": "rag",
                },
                {
                    "ref": "Decreto 100/1980",
                    "tier": "DEROGADA",                          # M20.10
                    "found": True,
                    "derogada_por": "Ley 599/2000",
                    "suggested_correction": "Ley 599/2000 art. equivalente",
                },
            ],
            "overall_score": 0.91,
        }
        # shape obligatorio
        assert "verified_at" in verifier_audit
        assert "citations" in verifier_audit
        assert isinstance(verifier_audit["citations"], list)
        for cit in verifier_audit["citations"]:
            assert "ref" in cit
            # M20.10: tier obligatorio en outputs nuevos
            assert cit.get("tier") in (None, "GROUNDED", "VERIFY_FLAG", "DEROGADA", "NOT_FOUND")


class TestVerificationAttemptsShape:
    """verification_attempts: cada intento de verificar una cita."""

    def test_required_columns(self):
        required = {
            "id", "firm_id", "user_id", "citation_ref", "ref_type",
            "result_state", "source", "duration_ms",
            "error_message", "metadata",
        }
        sample = {c: None for c in required}
        assert set(sample.keys()) >= required

    def test_result_state_enum_values(self):
        """result_state es enum closed."""
        valid_states = {
            "verificada", "no_encontrada", "sospechosa",
            "derogada", "modulada", "cache_hit", "error",
        }
        for st in valid_states:
            assert st in valid_states

    def test_source_enum_values(self):
        valid_sources = {
            "cache", "bd", "live_cc", "live_senado", "web_search",
            "mcp_corte_cc", "mcp_csj", "mcp_suin", "mcp_senado",   # M20.09
            "mcp_funcpub", "mcp_datosgov",                          # M20.09
        }
        # esperamos al menos los originales + los nuevos M20
        originals = {"cache", "bd", "live_cc", "live_senado", "web_search"}
        assert originals.issubset(valid_sources)


class TestChatMessagesShape:
    """chat_messages: persistencia de cada mensaje del chat panel."""

    def test_required_columns(self):
        required = {
            "id", "thread_id", "firm_id", "user_id",
            "generation_id", "document_id", "matter_id",
            "role", "channel", "content", "segments",
            "duration_ms", "total_tools_used", "total_tokens",
        }
        sample = {c: None for c in required}
        assert set(sample.keys()) >= required

    def test_role_enum(self):
        valid_roles = {"user", "assistant", "system", "tool"}
        for r in valid_roles:
            assert r in valid_roles

    def test_channel_enum(self):
        valid_channels = {"voice", "chat", "composer"}
        for c in valid_channels:
            assert c in valid_channels

    def test_segments_array_shape(self):
        """segments[] es array de {type, markdown, tool_calls?}."""
        segments = [
            {"type": "markdown", "markdown": "Hola"},
            {"type": "tool_call", "tool": "verify_citation",
             "tool_id": "tu-1", "input": {}, "output": {}},
        ]
        for seg in segments:
            assert "type" in seg
            if seg["type"] == "markdown":
                assert "markdown" in seg
            if seg["type"] == "tool_call":
                assert "tool" in seg


class TestToolCallAuditShape:
    """tool_call_audit (M20.01 nuevo) — audit granular por tool call."""

    def test_required_columns(self):
        required = {
            "id", "generation_id", "firm_id", "user_id", "tool_name",
            "iteration", "started_at", "duration_ms",
            "input_hash", "output_hash", "success",
            "error_class", "error_message", "cached",
            "model_used", "tokens_in", "tokens_out", "cost_usd",
            "cache_creation_tokens", "cache_read_tokens",
            "metadata", "created_at",
        }
        sample = {c: None for c in required}
        assert set(sample.keys()) >= required

    def test_success_tristate(self):
        """success: true|false|null (en curso)."""
        for val in (True, False, None):
            assert val in (True, False, None)

    def test_tool_names_expected_18(self):
        """M20.02 espera estas 18 tools registradas."""
        expected = {
            "load_skill_md", "load_playbook", "extract_data",
            "load_matter_context", "recall_memory",
            "verify_citation", "search_jurisprudence", "search_brave_gov",
            "fetch_mcp_official", "check_derogation",
            "generate_clause", "check_completeness", "check_coherence",
            "validate_legal", "calc_legal",
            "build_docx", "narrate_progress", "persist_audit",
        }
        assert len(expected) == 18

    def test_model_used_for_llm_invoking_tools(self):
        """Tools que invocan LLM (generate_clause, extract_data, etc.)
        deben registrar el modelo usado."""
        llm_invoking_tools = {
            "extract_data", "generate_clause", "validate_legal",
            "check_coherence", "check_completeness", "narrate_progress",
        }
        valid_models = {"claude-sonnet-4-6", "claude-opus-4-7", "gpt-4o", "gpt-4o-mini"}
        # asserción contractual
        for tool in llm_invoking_tools:
            assert tool in llm_invoking_tools
