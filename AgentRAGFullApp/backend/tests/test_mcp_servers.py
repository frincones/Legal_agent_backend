"""Sprint M20.09 · Tests de los 6 MCP CO clients (sin red, mocked HTTP)."""
from __future__ import annotations

import pytest

from lex.mcp import available_servers, get_mcp_client
from lex.mcp.base import BaseMCPClient, MCPResult, RateLimiter


class TestRegistry:
    def test_available_servers_count(self):
        assert len(available_servers()) == 6

    def test_available_servers_names(self):
        assert set(available_servers()) == {
            "corte_cc", "csj", "suin", "senado", "funcpub", "datosgov",
        }

    def test_get_each_client_returns_instance(self):
        for name in available_servers():
            client = get_mcp_client(name)
            assert client is not None, f"{name} no se pudo instanciar"
            assert isinstance(client, BaseMCPClient)
            assert client.server_name == name

    def test_get_unknown_server(self):
        assert get_mcp_client("nonexistent_server") is None


class TestMethodsRegistered:
    def test_corte_cc_methods(self):
        c = get_mcp_client("corte_cc")
        assert set(c.methods.keys()) == {"fetch_sentencia", "search_tema", "latest_sentencias"}

    def test_csj_methods(self):
        c = get_mcp_client("csj")
        assert "fetch_sentencia" in c.methods
        assert "search_radicado" in c.methods

    def test_suin_methods(self):
        c = get_mcp_client("suin")
        assert "fetch_norma" in c.methods
        assert "search_articulo" in c.methods
        assert "vigencia_check" in c.methods

    def test_senado_methods(self):
        c = get_mcp_client("senado")
        assert "fetch_ley" in c.methods

    def test_funcpub_methods(self):
        c = get_mcp_client("funcpub")
        assert "fetch_concepto" in c.methods

    def test_datosgov_methods(self):
        c = get_mcp_client("datosgov")
        assert "query_dataset" in c.methods


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_acquire_below_limit_no_delay(self):
        import time
        rl = RateLimiter(max_per_minute=100)
        t0 = time.perf_counter()
        for _ in range(5):
            await rl.acquire()
        assert time.perf_counter() - t0 < 0.1   # casi instantáneo


class TestCallDispatch:
    @pytest.mark.asyncio
    async def test_unknown_method_returns_error(self):
        c = get_mcp_client("corte_cc")
        result = await c.call("metodo_inexistente")
        assert isinstance(result, MCPResult)
        assert result.success is False
        assert "no soportado" in result.error

    @pytest.mark.asyncio
    async def test_corte_cc_fetch_sentencia_invalid_tipo(self):
        c = get_mcp_client("corte_cc")
        # tipo inválido → handler levanta ValueError → wrapped en MCPResult
        result = await c.call("fetch_sentencia", {"tipo": "X", "numero": 1, "año": 2025})
        assert result.success is False
        assert "T|C|SU" in result.error

    @pytest.mark.asyncio
    async def test_suin_search_articulo_returns_canonical_url(self):
        c = get_mcp_client("suin")
        result = await c.call("search_articulo", {"codigo": "CC", "articulo": 2142})
        assert result.success is True
        assert "fuente_url" in result.data
        assert "suin" in result.data["fuente_url"] or "secretariasenado" in result.data["fuente_url"]

    @pytest.mark.asyncio
    async def test_cache_returns_cached_on_2nd_call(self):
        c = get_mcp_client("suin")
        r1 = await c.call("search_articulo", {"codigo": "CC", "articulo": 2142})
        r2 = await c.call("search_articulo", {"codigo": "CC", "articulo": 2142})
        assert r1.success
        assert r2.success
        # 2da llamada debe venir de cache
        assert r2.cached is True

    @pytest.mark.asyncio
    async def test_different_params_different_cache(self):
        c = get_mcp_client("suin")
        r1 = await c.call("search_articulo", {"codigo": "CC", "articulo": 2142})
        r2 = await c.call("search_articulo", {"codigo": "CC", "articulo": 2199})
        # ambas son llamadas distintas (cache keys distintos)
        # r2 NO debe ser cached del r1
        assert r1.data != r2.data
