"""Tests for G.15 — doctor checks for Sub-project G subsystems.

Verifies that ``_check_g_subsystems`` reports correctly for each of:

- cron storage (skip when missing, pass with job count, warn on unreadable)
- webhook tokens (skip / pass with counts)
- cost-guard limits (skip / warn when no caps / pass when caps set)
- voice TTS/STT key (pass / skip on env var)
- oauth store (skip / pass / warn on permission drift)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencomputer.doctor import _check_g_subsystems


@pytest.fixture(autouse=True)
def isolate_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    # Reset the cost-guard singleton so each test gets a fresh one
    from opencomputer.cost_guard.guard import _reset_default_guard_for_tests
    _reset_default_guard_for_tests()
    yield tmp_path
    _reset_default_guard_for_tests()


def _check_named(checks, name):
    return next((c for c in checks if c.name == name), None)


class TestEmptyProfile:
    """Fresh profile — every G subsystem returns 'skip'."""

    def test_all_checks_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        checks = _check_g_subsystems()
        names = ["cron storage", "webhook tokens", "cost-guard limits", "voice TTS/STT key", "oauth store"]
        for name in names:
            c = _check_named(checks, name)
            assert c is not None, f"missing {name!r}"
            assert c.status == "skip", f"{name}: expected skip, got {c.status}"


class TestCronCheck:
    def test_pass_with_job_count(self, isolate_profile: Path) -> None:
        cron_dir = isolate_profile / "cron"
        cron_dir.mkdir()
        (cron_dir / "jobs.json").write_text(
            json.dumps({"jobs": [{"id": "a"}, {"id": "b"}]})
        )
        check = _check_named(_check_g_subsystems(), "cron storage")
        assert check.status == "pass"
        assert "2 job" in check.detail

    def test_warn_on_unreadable(self, isolate_profile: Path) -> None:
        cron_dir = isolate_profile / "cron"
        cron_dir.mkdir()
        (cron_dir / "jobs.json").write_text("not valid json {")
        check = _check_named(_check_g_subsystems(), "cron storage")
        assert check.status == "warn"


class TestWebhookCheck:
    def test_pass_with_token_counts(self, isolate_profile: Path) -> None:
        (isolate_profile / "webhook_tokens.json").write_text(
            json.dumps({
                "tokens": {
                    "a": {"revoked": False},
                    "b": {"revoked": False},
                    "c": {"revoked": True},
                }
            })
        )
        check = _check_named(_check_g_subsystems(), "webhook tokens")
        assert check.status == "pass"
        assert "2 active" in check.detail
        assert "3 total" in check.detail

    def test_warn_on_corrupted(self, isolate_profile: Path) -> None:
        (isolate_profile / "webhook_tokens.json").write_text("garbage")
        check = _check_named(_check_g_subsystems(), "webhook tokens")
        assert check.status == "warn"


class TestCostGuardCheck:
    def test_warn_when_usage_tracked_but_no_caps(self, isolate_profile: Path) -> None:
        from opencomputer.cost_guard import get_default_guard

        get_default_guard().record_usage("openai", cost_usd=0.01)
        check = _check_named(_check_g_subsystems(), "cost-guard limits")
        # Usage tracked but no limit set → warn
        assert check.status == "warn"
        assert "unguarded" in check.detail.lower()

    def test_pass_when_limits_set(self, isolate_profile: Path) -> None:
        from opencomputer.cost_guard import get_default_guard

        get_default_guard().set_limit("openai", daily=5.0)
        check = _check_named(_check_g_subsystems(), "cost-guard limits")
        assert check.status == "pass"
        assert "1 provider" in check.detail


class TestVoiceCheck:
    def test_pass_when_openai_key_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-test-key")
        check = _check_named(_check_g_subsystems(), "voice TTS/STT key")
        assert check.status == "pass"

    def test_skip_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        check = _check_named(_check_g_subsystems(), "voice TTS/STT key")
        assert check.status == "skip"


class TestOAuthCheck:
    def test_pass_with_token_count(self, isolate_profile: Path) -> None:
        from opencomputer.mcp.oauth import paste_token

        paste_token(provider="github", access_token="ghp_x")
        paste_token(provider="google", access_token="abc")
        check = _check_named(_check_g_subsystems(), "oauth store")
        assert check.status == "pass"
        assert "2 token" in check.detail
