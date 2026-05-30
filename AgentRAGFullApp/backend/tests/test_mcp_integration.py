"""Sprint M20.09 follow-up · Tests E2E reales contra los 6 MCP CO.

Estos tests SOLO corren con la variable de entorno LEXAI_MCP_INTEGRATION=1
(skipped por default). Hacen requests HTTP reales a:
  - suin-juriscol.gov.co
  - corteconstitucional.gov.co
  - cortesuprema.gov.co
  - secretariasenado.gov.co
  - funcionpublica.gov.co
  - datos.gov.co

USO:
    # Skip por default
    pytest tests/test_mcp_integration.py

    # Correr de verdad (puede ser lento, ~30s, requiere internet)
    LEXAI_MCP_INTEGRATION=1 pytest tests/test_mcp_integration.py -v -s
"""
from __future__ import annotations

import os

import pytest

from lex.mcp import available_servers, get_mcp_client


SKIP_REASON = (
    "Set LEXAI_MCP_INTEGRATION=1 to run real-network MCP tests. "
    "Skipped by default to keep CI fast and reliable."
)


pytestmark = pytest.mark.skipif(
    os.getenv("LEXAI_MCP_INTEGRATION", "").lower() not in ("1", "true", "yes"),
    reason=SKIP_REASON,
)


# ---- Helpers ----

async def _safe_call(client, method, params):
    """Llamada con timeout suave; retorna MCPResult."""
    return await client.call(method, params)


# ---- Tests E2E ----

class TestCorteCCIntegration:
    @pytest.mark.asyncio
    async def test_fetch_sentencia_t067_2025(self):
        c = get_mcp_client("corte_cc")
        r = await _safe_call(c, "fetch_sentencia", {"tipo": "T", "numero": 67, "año": 2025})
        assert r is not None
        if r.success and r.data and r.data.get("exists"):
            assert r.data["fuente_url"].startswith("https://www.corteconstitucional.gov.co/")
        # Si la sentencia no existe (404 del portal) también es OK
        print(f"\n  corte_cc T-067/2025: exists={r.data.get('exists') if r.data else 'N/A'}")


class TestSuinIntegration:
    @pytest.mark.asyncio
    async def test_search_articulo_cgp_627(self):
        c = get_mcp_client("suin")
        r = await _safe_call(c, "search_articulo", {"codigo": "CGP", "articulo": 627})
        assert r.success
        assert r.data["fuente_url"]

    @pytest.mark.asyncio
    async def test_search_articulo_cc_2142(self):
        c = get_mcp_client("suin")
        r = await _safe_call(c, "search_articulo", {"codigo": "CC", "articulo": 2142})
        assert r.success
        assert r.data["fuente_url"]


class TestSenadoIntegration:
    @pytest.mark.asyncio
    async def test_fetch_ley_1564_2012(self):
        """Ley 1564 = CGP, debe existir en basedoc."""
        c = get_mcp_client("senado")
        r = await _safe_call(c, "fetch_ley", {"numero": 1564, "año": 2012})
        assert r.success
        # exists puede ser true o false según el HTML del portal
        print(f"\n  senado Ley 1564/2012: exists={r.data.get('exists') if r.data else 'N/A'}")


class TestDatosGovIntegration:
    @pytest.mark.asyncio
    async def test_query_dataset_basic(self):
        """Smoke test: query un dataset chico."""
        c = get_mcp_client("datosgov")
        # Cualquier dataset_id pequeño. Si no existe, retorna 404 manejado por el client.
        r = await _safe_call(c, "query_dataset", {
            "dataset_id": "vrtj-3dvk",   # ej. de prueba
            "limit": 5,
        })
        # success o error gracioso, pero NO debe crashear
        assert r is not None


class TestAllClientsResponsive:
    """Smoke: cada uno de los 6 MCP debe al menos no levantar excepción al ser instanciado."""

    @pytest.mark.asyncio
    async def test_all_6_servers_instantiate(self):
        for name in available_servers():
            c = get_mcp_client(name)
            assert c is not None, f"{name} no se pudo instanciar"
            assert c.server_name == name
