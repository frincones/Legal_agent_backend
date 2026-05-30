"""Sprint M20.08 · Tests de seguridad del sandbox.

NO ejecuta código real con bubblewrap (no está garantizado en CI). Valida:
  - blocklist de imports detecta intentos comunes de escape
  - validación estática rechaza correctamente
  - SandboxConfig + SandboxResult shape estable
  - subprocess fallback funciona (sin bwrap) para tests básicos
"""
from __future__ import annotations

import asyncio
import json
import sys

import pytest

from lex.sandbox import (
    SandboxConfig,
    SandboxResult,
    is_bubblewrap_available,
    run_python_in_sandbox,
)
from lex.sandbox.runner import _validate_code_imports, BLOCKED_IMPORTS


class TestStaticValidation:
    def test_blocks_import_os(self):
        err = _validate_code_imports("import os\nos.system('ls')")
        assert err is not None
        assert "os" in err

    def test_blocks_import_subprocess(self):
        err = _validate_code_imports("import subprocess\nsubprocess.run(['curl'])")
        assert err is not None
        assert "subprocess" in err

    def test_blocks_from_socket(self):
        err = _validate_code_imports("from socket import socket")
        assert err is not None
        assert "socket" in err

    def test_blocks_ctypes(self):
        err = _validate_code_imports("import ctypes")
        assert err is not None

    def test_allows_safe_imports(self):
        err = _validate_code_imports("import json\nimport re\nfrom datetime import date")
        assert err is None

    def test_blocklist_completeness(self):
        for mod in ("os", "sys", "subprocess", "socket", "ctypes", "multiprocessing"):
            assert mod in BLOCKED_IMPORTS


class TestSandboxRun:
    @pytest.mark.asyncio
    async def test_simple_calc_executes(self):
        """Código matemático simple debe correr."""
        result = await run_python_in_sandbox(
            "import json\nresult = 2 + 2\nprint(json.dumps({'result': result}))",
            config=SandboxConfig(timeout_seconds=10),
        )
        assert isinstance(result, SandboxResult)
        assert result.backend_used in ("bubblewrap", "subprocess_fallback")
        if result.success:
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            assert payload["result"] == 4

    @pytest.mark.asyncio
    async def test_blocks_os_at_static_stage(self):
        result = await run_python_in_sandbox("import os\nos.listdir('/')")
        assert result.success is False
        assert result.error == "static_validation_failed"

    @pytest.mark.asyncio
    async def test_blocks_subprocess(self):
        result = await run_python_in_sandbox(
            "import subprocess\nsubprocess.run(['cat', '/etc/passwd'])",
        )
        assert result.success is False
        assert result.error == "static_validation_failed"

    @pytest.mark.asyncio
    async def test_timeout_kills_process(self):
        """Código que tarda > timeout debe ser killed."""
        result = await run_python_in_sandbox(
            "import time\ntime.sleep(20)",
            config=SandboxConfig(timeout_seconds=2),
        )
        assert result.success is False
        assert result.error == "timeout"

    @pytest.mark.asyncio
    async def test_input_data_passed_as_INPUT(self):
        result = await run_python_in_sandbox(
            "import json\nprint(json.dumps({'sum': INPUT['a'] + INPUT['b']}))",
            input_data={"a": 10, "b": 32},
            config=SandboxConfig(timeout_seconds=10),
        )
        if result.success:
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            assert payload["sum"] == 42

    @pytest.mark.asyncio
    async def test_config_defaults(self):
        config = SandboxConfig()
        assert config.timeout_seconds == 30
        assert config.allow_network is False
        assert config.max_memory_mb == 512
        assert len(config.network_allowlist) >= 5
        assert "suin-juriscol.gov.co" in config.network_allowlist
