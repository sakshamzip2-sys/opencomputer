"""Tests for opencomputer.cost_guard — per-provider budget tracking + caps.

G.8 / Tier 2.17: prerequisite for voice (2.10) and any paid-API integration.
Verifies record/check/limits round-trip, daily + monthly bucketing, decision
flagging at thresholds, retention pruning, profile isolation, file mode.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.cost_guard import (
    BudgetDecision,
    CostGuard,
    ProviderUsage,
    get_default_guard,
)
from opencomputer.cost_guard.guard import (
    _RETENTION_DAYS,
    _reset_default_guard_for_tests,
)


@pytest.fixture(autouse=True)
def isolate_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    _reset_default_guard_for_tests()
    yield tmp_path
    _reset_default_guard_for_tests()


@pytest.fixture
def guard(tmp_path: Path) -> CostGuard:
    return CostGuard(storage_path=tmp_path / "cost_guard.json")


class TestRecordUsage:
    def test_first_record_creates_file(self, guard: CostGuard) -> None:
        guard.record_usage("openai", cost_usd=0.015, operation="tts")
        usage = guard.current_usage("openai")
        assert usage[0].daily_used == pytest.approx(0.015)

    def test_multiple_records_sum(self, guard: CostGuard) -> None:
        for _ in range(10):
            guard.record_usage("openai", cost_usd=0.001)
        usage = guard.current_usage("openai")
        assert usage[0].daily_used == pytest.approx(0.010)

    def test_negative_cost_rejected(self, guard: CostGuard) -> None:
        with pytest.raises(ValueError):
            guard.record_usage("openai", cost_usd=-1.0)

    def test_provider_normalised_lowercase(self, guard: CostGuard) -> None:
        guard.record_usage("OpenAI", cost_usd=0.01)
        usage = guard.current_usage("openai")
        assert usage[0].daily_used == pytest.approx(0.01)

    def test_operation_label_surfaces(self, guard: CostGuard) -> None:
        guard.record_usage("openai", cost_usd=0.01, operation="tts")
        guard.record_usage("openai", cost_usd=0.02, operation="completion")
        usage = guard.current_usage("openai")
        assert usage[0].operations_today == {"tts": pytest.approx(0.01), "completion": pytest.approx(0.02)}


class TestCheckBudget:
    def test_no_limits_always_allowed(self, guard: CostGuard) -> None:
        d = guard.check_budget("openai", projected_cost_usd=999.0)
        assert d.allowed
        assert "no limits" in d.reason

    def test_under_daily_limit_allowed(self, guard: CostGuard) -> None:
        guard.set_limit("openai", daily=5.0)
        d = guard.check_budget("openai", projected_cost_usd=1.0)
        assert d.allowed
        assert "daily" in d.reason

    def test_over_daily_limit_blocked(self, guard: CostGuard) -> None:
        guard.set_limit("openai", daily=1.0)
        guard.record_usage("openai", cost_usd=0.95)
        d = guard.check_budget("openai", projected_cost_usd=0.10)
        assert not d.allowed
        assert "daily" in d.reason

    def test_over_monthly_limit_blocked(self, guard: CostGuard) -> None:
        guard.set_limit("openai", monthly=10.0)
        guard.record_usage("openai", cost_usd=9.5)
        d = guard.check_budget("openai", projected_cost_usd=1.0)
        assert not d.allowed
        assert "monthly" in d.reason

    def test_decision_includes_used_and_limit(self, guard: CostGuard) -> None:
        guard.set_limit("openai", daily=5.0, monthly=50.0)
        guard.record_usage("openai", cost_usd=2.0)
        d = guard.check_budget("openai", projected_cost_usd=1.0)
        assert d.daily_used == pytest.approx(2.0)
        assert d.daily_limit == 5.0
        assert d.monthly_used == pytest.approx(2.0)
        assert d.monthly_limit == 50.0

    def test_negative_projected_rejected(self, guard: CostGuard) -> None:
        with pytest.raises(ValueError):
            guard.check_budget("openai", projected_cost_usd=-1.0)


class TestSetLimit:
    def test_set_then_clear(self, guard: CostGuard) -> None:
        guard.set_limit("openai", daily=5.0, monthly=50.0)
        usage = guard.current_usage("openai")
        assert usage[0].daily_limit == 5.0
        assert usage[0].monthly_limit == 50.0

        # Clear daily, keep monthly
        guard.set_limit("openai", daily=None, monthly=50.0)
        usage = guard.current_usage("openai")
        assert usage[0].daily_limit is None
        assert usage[0].monthly_limit == 50.0

    def test_clear_both_drops_provider(self, guard: CostGuard) -> None:
        guard.set_limit("openai", daily=5.0)
        # Clear it
        guard.set_limit("openai", daily=None, monthly=None)
        # No usage either; current_usage(provider) still returns one entry
        # (because we asked for it explicitly), but list-all returns nothing.
        listed = guard.current_usage()
        assert listed == []


class TestCurrentUsage:
    def test_empty_returns_empty_list(self, guard: CostGuard) -> None:
        assert guard.current_usage() == []

    def test_lists_all_providers(self, guard: CostGuard) -> None:
        guard.record_usage("openai", cost_usd=0.1)
        guard.record_usage("anthropic", cost_usd=0.2)
        guard.set_limit("elevenlabs", daily=1.0)
        names = sorted(p.provider for p in guard.current_usage())
        assert names == ["anthropic", "elevenlabs", "openai"]


class TestReset:
    def test_reset_clears_usage_keeps_limits(self, guard: CostGuard) -> None:
        guard.set_limit("openai", daily=5.0)
        guard.record_usage("openai", cost_usd=2.0)
        guard.reset()
        usage = guard.current_usage("openai")
        assert usage[0].daily_used == 0.0
        # Limits still set
        assert usage[0].daily_limit == 5.0

    def test_reset_specific_provider(self, guard: CostGuard) -> None:
        guard.record_usage("openai", cost_usd=1.0)
        guard.record_usage("anthropic", cost_usd=2.0)
        guard.reset("openai")
        assert guard.current_usage("openai")[0].daily_used == 0.0
        assert guard.current_usage("anthropic")[0].daily_used == pytest.approx(2.0)


class TestRetentionPrune:
    def test_old_days_pruned(self, guard: CostGuard, tmp_path: Path) -> None:
        # Hand-craft a state file with stale days
        old_day = (
            datetime.now(UTC) - timedelta(days=_RETENTION_DAYS + 5)
        ).strftime("%Y-%m-%d")
        recent_day = datetime.now(UTC).strftime("%Y-%m-%d")
        state_file = tmp_path / "cost_guard.json"
        state_file.write_text(
            json.dumps({
                "version": 1,
                "limits": {},
                "usage": {
                    "openai": {
                        old_day: [{"ts": time.time() - 86400 * 95, "operation": "x", "cost": 1.0}],
                        recent_day: [{"ts": time.time(), "operation": "x", "cost": 0.5}],
                    }
                },
            })
        )

        # Recording any new usage triggers prune
        guard.record_usage("openai", cost_usd=0.001)

        # Reload and check the old day is gone
        with open(state_file) as fh:
            data = json.load(fh)
        assert old_day not in data["usage"]["openai"]
        assert recent_day in data["usage"]["openai"]


class TestStorageHygiene:
    def test_file_mode_0600(self, guard: CostGuard, tmp_path: Path) -> None:
        guard.record_usage("openai", cost_usd=0.01)
        f = tmp_path / "cost_guard.json"
        if os.name != "nt":
            assert oct(f.stat().st_mode)[-3:] == "600"

    def test_profile_isolated(self, guard: CostGuard, tmp_path: Path) -> None:
        guard.record_usage("openai", cost_usd=0.01)
        assert (tmp_path / "cost_guard.json").exists()


class TestDefaultGuard:
    def test_singleton(self) -> None:
        a = get_default_guard()
        b = get_default_guard()
        assert a is b


class TestCLI:
    @pytest.fixture
    def runner(self) -> CliRunner:
        return CliRunner()

    def test_show_empty(self, runner: CliRunner) -> None:
        from opencomputer.cli_cost import cost_app

        result = runner.invoke(cost_app, ["show"])
        assert result.exit_code == 0
        # 2026-04-28: empty-state pass — "No usage recorded" → teaching block
        assert "Cost tracking (empty)" in result.stdout

    def test_set_limit_then_show(self, runner: CliRunner) -> None:
        from opencomputer.cli_cost import cost_app

        result = runner.invoke(
            cost_app, ["set-limit", "--provider", "openai", "--daily", "5.0"]
        )
        assert result.exit_code == 0
        assert "openai" in result.stdout

        result2 = runner.invoke(cost_app, ["show"])
        assert "openai" in result2.stdout
        assert "$5.0000" in result2.stdout

    def test_set_limit_requires_at_least_one(self, runner: CliRunner) -> None:
        from opencomputer.cli_cost import cost_app

        result = runner.invoke(cost_app, ["set-limit", "--provider", "openai"])
        assert result.exit_code == 2

    def test_reset_with_yes(self, runner: CliRunner) -> None:
        from opencomputer.cli_cost import cost_app

        # Set up usage
        guard = get_default_guard()
        guard.record_usage("openai", cost_usd=1.0)
        result = runner.invoke(cost_app, ["reset", "--yes"])
        assert result.exit_code == 0
        assert "Reset" in result.stdout
        # Verify cleared
        assert get_default_guard().current_usage("openai")[0].daily_used == 0.0


class TestDataclassShapes:
    def test_budget_decision_is_frozen(self) -> None:
        d = BudgetDecision(
            allowed=True,
            reason="ok",
            daily_used=0.0,
            daily_limit=None,
            monthly_used=0.0,
            monthly_limit=None,
        )
        with pytest.raises((AttributeError, Exception)):  # frozen=True
            d.allowed = False  # type: ignore[misc]

    def test_provider_usage_shape(self) -> None:
        u = ProviderUsage(
            provider="openai",
            daily_used=1.0,
            monthly_used=5.0,
            daily_limit=10.0,
            monthly_limit=50.0,
        )
        assert u.provider == "openai"
        assert u.operations_today == {}
