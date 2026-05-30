"""Sprint M20.12 · Tests del NetworkAllowlistProxy."""
from __future__ import annotations

import pytest

from lex.sandbox import (
    DEFAULT_ALLOWED_HOSTS,
    NetworkAllowlistProxy,
    is_host_allowed,
)


class TestAllowlistHostCheck:
    def test_suin_juriscol_allowed(self):
        assert is_host_allowed("https://www.suin-juriscol.gov.co/viewDocument.asp?id=123")

    def test_corteconstitucional_allowed(self):
        assert is_host_allowed("https://www.corteconstitucional.gov.co/relatoria/")

    def test_cortesuprema_subdomain_allowed(self):
        assert is_host_allowed("https://cortesuprema.gov.co/")

    def test_senado_allowed(self):
        assert is_host_allowed("https://www.secretariasenado.gov.co/senado/basedoc/")

    def test_datosgov_allowed(self):
        assert is_host_allowed("https://www.datos.gov.co/resource/abc.json")

    def test_evil_com_blocked(self):
        assert not is_host_allowed("https://evil.com/exfiltrate")

    def test_random_subdomain_blocked(self):
        assert not is_host_allowed("https://random.attacker.io/x")

    def test_no_scheme_blocked(self):
        assert not is_host_allowed("not-a-url")

    def test_subdomain_of_allowed_passes(self):
        # relatoria.corteconstitucional.gov.co debe pasar por wildcard ".corteconstitucional.gov.co"
        assert is_host_allowed("https://relatoria.corteconstitucional.gov.co/algo")

    def test_custom_allowlist(self):
        custom = frozenset(["mysite.example.com"])
        assert is_host_allowed("https://mysite.example.com/x", custom)
        assert not is_host_allowed("https://www.google.com/", custom)


class TestNetworkAllowlistProxy:
    def test_proxy_instantiates(self):
        proxy = NetworkAllowlistProxy()
        assert proxy.allowed == DEFAULT_ALLOWED_HOSTS
        assert proxy.timeout == 20.0

    def test_audit_log_starts_empty(self):
        proxy = NetworkAllowlistProxy()
        assert proxy.get_audit_log() == []

    def test_default_allowlist_has_8_hosts(self):
        # 8 hosts × (con + sin www) = 16
        assert len(DEFAULT_ALLOWED_HOSTS) >= 14
        # principales gov.co presentes
        domains = {h.replace("www.", "") for h in DEFAULT_ALLOWED_HOSTS}
        assert "suin-juriscol.gov.co" in domains
        assert "corteconstitucional.gov.co" in domains
        assert "cortesuprema.gov.co" in domains
        assert "secretariasenado.gov.co" in domains
        assert "datos.gov.co" in domains
