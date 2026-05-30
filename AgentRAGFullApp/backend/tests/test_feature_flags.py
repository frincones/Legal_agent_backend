"""Sprint M20.01 · Tests de feature_flags.should_use_lean.

Cubre:
  - default off
  - override total USE_LEAN_ORCHESTRATOR=true
  - allowlist firms
  - hash-based percentage determinístico (consistencia intra-firm)
  - edge cases (firm_id none, percentage inválido)
"""
from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

from utils.feature_flags import (
    should_use_lean,
    orchestrator_kind,
    flags_snapshot,
    _hash_firm_to_bucket,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Limpia env vars antes de cada test."""
    for k in ("USE_LEAN_ORCHESTRATOR", "LEAN_ORCHESTRATOR_FIRMS",
              "LEAN_ORCHESTRATOR_PERCENTAGE"):
        monkeypatch.delenv(k, raising=False)
    yield


class TestDefaults:
    def test_default_off_without_any_env(self):
        assert should_use_lean(firm_id=uuid4()) is False

    def test_default_off_without_firm_id(self):
        assert should_use_lean() is False

    def test_orchestrator_kind_default(self):
        assert orchestrator_kind(firm_id=uuid4()) == "legacy"


class TestOverrideTotal:
    def test_use_lean_true_overrides_everything(self, monkeypatch):
        monkeypatch.setenv("USE_LEAN_ORCHESTRATOR", "true")
        # incluso sin firm_id
        assert should_use_lean() is True
        # incluso con percentage 0
        monkeypatch.setenv("LEAN_ORCHESTRATOR_PERCENTAGE", "0")
        assert should_use_lean(firm_id=uuid4()) is True

    def test_use_lean_false_explicit(self, monkeypatch):
        monkeypatch.setenv("USE_LEAN_ORCHESTRATOR", "false")
        assert should_use_lean(firm_id=uuid4()) is False

    def test_use_lean_variants(self, monkeypatch):
        for true_val in ("true", "True", "TRUE", "1", "yes", "on"):
            monkeypatch.setenv("USE_LEAN_ORCHESTRATOR", true_val)
            assert should_use_lean() is True, f"failed for {true_val}"
        for false_val in ("false", "0", "no", "off", ""):
            monkeypatch.setenv("USE_LEAN_ORCHESTRATOR", false_val)
            assert should_use_lean() is False, f"failed for {false_val}"


class TestAllowlist:
    def test_firm_in_allowlist(self, monkeypatch):
        firm_qa = uuid4()
        monkeypatch.setenv("LEAN_ORCHESTRATOR_FIRMS", str(firm_qa))
        assert should_use_lean(firm_id=firm_qa) is True

    def test_firm_not_in_allowlist(self, monkeypatch):
        firm_qa = uuid4()
        firm_other = uuid4()
        monkeypatch.setenv("LEAN_ORCHESTRATOR_FIRMS", str(firm_qa))
        assert should_use_lean(firm_id=firm_other) is False

    def test_allowlist_multiple_firms(self, monkeypatch):
        f1, f2, f3 = uuid4(), uuid4(), uuid4()
        monkeypatch.setenv("LEAN_ORCHESTRATOR_FIRMS", f"{f1}, {f2}")
        assert should_use_lean(firm_id=f1) is True
        assert should_use_lean(firm_id=f2) is True
        assert should_use_lean(firm_id=f3) is False

    def test_allowlist_case_insensitive(self, monkeypatch):
        f1 = uuid4()
        monkeypatch.setenv("LEAN_ORCHESTRATOR_FIRMS", str(f1).upper())
        assert should_use_lean(firm_id=f1) is True


class TestPercentageRollout:
    def test_percentage_0_no_one(self, monkeypatch):
        monkeypatch.setenv("LEAN_ORCHESTRATOR_PERCENTAGE", "0")
        # ningún firm cae en lean
        assert all(should_use_lean(firm_id=uuid4()) is False for _ in range(50))

    def test_percentage_100_everyone(self, monkeypatch):
        monkeypatch.setenv("LEAN_ORCHESTRATOR_PERCENTAGE", "100")
        assert all(should_use_lean(firm_id=uuid4()) is True for _ in range(50))

    def test_percentage_50_roughly_half(self, monkeypatch):
        monkeypatch.setenv("LEAN_ORCHESTRATOR_PERCENTAGE", "50")
        results = [should_use_lean(firm_id=uuid4()) for _ in range(500)]
        true_count = sum(results)
        # tolerancia ±15% (500 samples)
        assert 200 < true_count < 300, f"expected ~250, got {true_count}"

    def test_consistency_same_firm_same_bucket(self, monkeypatch):
        """Una misma firm siempre cae en mismo lado en runs consecutivos."""
        monkeypatch.setenv("LEAN_ORCHESTRATOR_PERCENTAGE", "30")
        f1 = uuid4()
        decision_1 = should_use_lean(firm_id=f1)
        for _ in range(10):
            assert should_use_lean(firm_id=f1) == decision_1

    def test_percentage_invalid_falls_to_default(self, monkeypatch):
        monkeypatch.setenv("LEAN_ORCHESTRATOR_PERCENTAGE", "not-a-number")
        # falla silenciosamente → 0 → legacy
        assert should_use_lean(firm_id=uuid4()) is False

    def test_percentage_negative_clamped(self, monkeypatch):
        monkeypatch.setenv("LEAN_ORCHESTRATOR_PERCENTAGE", "-50")
        assert should_use_lean(firm_id=uuid4()) is False

    def test_percentage_above_100_clamped(self, monkeypatch):
        monkeypatch.setenv("LEAN_ORCHESTRATOR_PERCENTAGE", "200")
        assert should_use_lean(firm_id=uuid4()) is True


class TestHashBucket:
    def test_hash_deterministic(self):
        f1 = uuid4()
        assert _hash_firm_to_bucket(f1) == _hash_firm_to_bucket(f1)

    def test_hash_range_0_99(self):
        for _ in range(100):
            b = _hash_firm_to_bucket(uuid4())
            assert 0 <= b < 100

    def test_hash_string_vs_uuid_equivalent(self):
        f1 = uuid4()
        assert _hash_firm_to_bucket(f1) == _hash_firm_to_bucket(str(f1))


class TestPrecedence:
    def test_override_wins_over_allowlist(self, monkeypatch):
        monkeypatch.setenv("USE_LEAN_ORCHESTRATOR", "true")
        monkeypatch.setenv("LEAN_ORCHESTRATOR_FIRMS", "")
        assert should_use_lean(firm_id=uuid4()) is True

    def test_allowlist_wins_over_percentage(self, monkeypatch):
        f_allow = uuid4()
        monkeypatch.setenv("LEAN_ORCHESTRATOR_FIRMS", str(f_allow))
        monkeypatch.setenv("LEAN_ORCHESTRATOR_PERCENTAGE", "0")
        assert should_use_lean(firm_id=f_allow) is True

    def test_percentage_only_used_when_no_allowlist_match(self, monkeypatch):
        f_allow = uuid4()
        f_other = uuid4()
        monkeypatch.setenv("LEAN_ORCHESTRATOR_FIRMS", str(f_allow))
        monkeypatch.setenv("LEAN_ORCHESTRATOR_PERCENTAGE", "100")
        assert should_use_lean(firm_id=f_allow) is True
        assert should_use_lean(firm_id=f_other) is True  # via percentage


class TestSnapshot:
    def test_snapshot_default(self):
        snap = flags_snapshot()
        assert snap["USE_LEAN_ORCHESTRATOR"] is False
        assert snap["LEAN_ORCHESTRATOR_FIRMS"] == []
        assert snap["LEAN_ORCHESTRATOR_PERCENTAGE"] == 0

    def test_snapshot_with_config(self, monkeypatch):
        f1 = str(uuid4()).lower()
        monkeypatch.setenv("USE_LEAN_ORCHESTRATOR", "true")
        monkeypatch.setenv("LEAN_ORCHESTRATOR_FIRMS", f1)
        monkeypatch.setenv("LEAN_ORCHESTRATOR_PERCENTAGE", "25")
        snap = flags_snapshot()
        assert snap["USE_LEAN_ORCHESTRATOR"] is True
        assert snap["LEAN_ORCHESTRATOR_FIRMS"] == [f1]
        assert snap["LEAN_ORCHESTRATOR_PERCENTAGE"] == 25
