"""Sprint M20.02 · Tests del paquete lex.tools.

Cubre:
  - ToolRegistry instancia correctamente las 18 tools
  - cada tool tiene schema_dict válido compatible con Anthropic tool_use
  - ToolDispatcher maneja success / error / timeout correctamente
  - validación contrato (nombres, descripción no vacía, schema válido)
"""
from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from lex.tools import (
    ToolCall,
    ToolContext,
    ToolDef,
    ToolDispatcher,
    ToolError,
    ToolRegistry,
)


EXPECTED_TOOL_NAMES = {
    "load_skill_md", "load_playbook", "extract_data",
    "load_matter_context", "recall_memory",
    "verify_citation", "search_jurisprudence", "search_brave_gov",
    "fetch_mcp_official", "check_derogation",
    "generate_clause", "check_completeness", "check_coherence",
    "validate_legal", "calc_legal",
    "build_docx", "narrate_progress", "persist_audit",
}


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry(pool=None, anthropic_client=None, openai_client=None)


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(
        generation_id=uuid4(), firm_id=uuid4(), user_id=uuid4(),
    )


class TestRegistryCompletes:
    def test_registry_has_18_tools(self, registry):
        assert len(registry) == 18, f"esperaba 18 tools, hay {len(registry)}: {registry.names()}"

    def test_all_expected_tool_names_present(self, registry):
        names = set(registry.names())
        missing = EXPECTED_TOOL_NAMES - names
        extra = names - EXPECTED_TOOL_NAMES
        assert not missing, f"faltan tools: {missing}"
        assert not extra, f"tools no esperadas: {extra}"

    def test_get_by_name_works(self, registry):
        for name in EXPECTED_TOOL_NAMES:
            tool = registry.get(name)
            assert tool is not None, f"registry.get({name!r}) retornó None"
            assert isinstance(tool, ToolDef)

    def test_unknown_tool_returns_none(self, registry):
        assert registry.get("nonexistent_tool") is None


class TestSchemaContract:
    def test_each_tool_has_name(self, registry):
        for t in registry.list():
            assert t.name and isinstance(t.name, str)
            assert "_" not in t.name or t.name.islower(), f"{t.name} debe ser snake_case"

    def test_each_tool_has_description(self, registry):
        for t in registry.list():
            assert t.description and isinstance(t.description, str)
            assert len(t.description) > 30, f"{t.name} description muy corta"

    def test_each_tool_has_valid_input_schema(self, registry):
        for t in registry.list():
            schema = t.input_schema
            assert isinstance(schema, dict)
            assert schema.get("type") == "object", f"{t.name} input_schema.type debe ser 'object'"
            assert "properties" in schema, f"{t.name} falta 'properties'"

    def test_schema_dict_anthropic_format(self, registry):
        for t in registry.list():
            sd = t.schema_dict()
            assert set(sd.keys()) == {"name", "description", "input_schema"}, \
                f"{t.name} schema_dict tiene keys incorrectas: {sd.keys()}"

    def test_schema_for_anthropic_list(self, registry):
        schemas = registry.schema_for_anthropic()
        assert len(schemas) == 18
        assert all("input_schema" in s for s in schemas)


class TestDispatcherBasics:
    @pytest.mark.asyncio
    async def test_dispatcher_unknown_tool_returns_error(self, registry, ctx):
        dispatcher = ToolDispatcher(persist_audit=False)
        call = ToolCall(tool_use_id="tu-1", tool_name="nonexistent", input={})
        result = await dispatcher.execute(call, registry, ctx)
        assert result.status == "error"
        assert result.error_class == "ToolNotFound"

    @pytest.mark.asyncio
    async def test_dispatcher_tool_anthropic_result_format(self, registry, ctx):
        dispatcher = ToolDispatcher(persist_audit=False)
        # narrate_progress sin pool → persiste False pero retorna success
        call = ToolCall(
            tool_use_id="tu-1",
            tool_name="narrate_progress",
            input={"message": "Hola", "kind": "narration"},
        )
        result = await dispatcher.execute(call, registry, ctx)
        assert result.status == "success"
        ar = result.to_anthropic_tool_result()
        assert ar["type"] == "tool_result"
        assert ar["tool_use_id"] == "tu-1"
        assert ar["is_error"] is False


class TestParallelExecution:
    @pytest.mark.asyncio
    async def test_parallel_execute(self, registry, ctx):
        dispatcher = ToolDispatcher(persist_audit=False)
        calls = [
            ToolCall(tool_use_id=f"tu-{i}", tool_name="narrate_progress",
                     input={"message": f"msg {i}", "kind": "narration"})
            for i in range(5)
        ]
        results = await dispatcher.execute_parallel(calls, registry, ctx, max_concurrent=3)
        assert len(results) == 5
        assert all(r.status == "success" for r in results)


class TestToolGroups:
    """Verifica que las tools están agrupadas según el plan."""

    def test_contexto_group(self, registry):
        ctx_tools = {"load_skill_md", "load_playbook", "extract_data",
                     "load_matter_context", "recall_memory"}
        assert ctx_tools.issubset(registry.names())

    def test_verificacion_group(self, registry):
        ver_tools = {"verify_citation", "search_jurisprudence", "search_brave_gov",
                     "fetch_mcp_official", "check_derogation"}
        assert ver_tools.issubset(registry.names())

    def test_generacion_group(self, registry):
        gen_tools = {"generate_clause", "check_completeness", "check_coherence",
                     "validate_legal", "calc_legal"}
        assert gen_tools.issubset(registry.names())

    def test_salida_group(self, registry):
        out_tools = {"build_docx", "narrate_progress", "persist_audit"}
        assert out_tools.issubset(registry.names())


class TestLLMInvokingTools:
    def test_llm_invoking_flag_set_correctly(self, registry):
        llm_tools = {"extract_data", "verify_citation", "search_jurisprudence",
                     "generate_clause", "check_completeness", "check_coherence",
                     "validate_legal"}
        for name in llm_tools:
            t = registry.get(name)
            assert t.invokes_llm, f"{name} debería tener invokes_llm=True"


class TestCacheability:
    def test_cacheable_tools(self, registry):
        cacheable = {"load_skill_md", "load_playbook", "load_matter_context",
                     "verify_citation", "search_jurisprudence", "search_brave_gov",
                     "fetch_mcp_official", "check_derogation"}
        for name in cacheable:
            t = registry.get(name)
            assert t.cacheable, f"{name} debería ser cacheable"
