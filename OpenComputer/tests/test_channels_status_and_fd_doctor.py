"""Tests for the final two deferral-cleanup items (2026-05-10):

1. ``oc channels status`` — pairing-status diagnostic for installed
   channel-kind extensions. Closes the audit gap "84 extensions installed,
   only Telegram paired" — operators now have a surface to see WHY each
   channel isn't paired (which env vars are missing).
2. ``oc doctor`` FD-limit + competing-daemon check. Closes the audit gap
   where the gateway briefly hit EMFILE and silently dropped pairing /
   sqlite operations.
"""
from __future__ import annotations

import pytest

# ─── _check_channel_credentials ──────────────────────────────────────


def test_channel_credentials_paired_when_required_env_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.cli_channels import _check_channel_credentials

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    status, _ = _check_channel_credentials("discord")
    assert status == "paired"


def test_channel_credentials_missing_when_required_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.cli_channels import _check_channel_credentials

    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    status, detail = _check_channel_credentials("discord")
    assert status == "missing"
    assert "DISCORD_BOT_TOKEN" in detail


def test_channel_credentials_unknown_channel_returns_unknown_status() -> None:
    from opencomputer.cli_channels import _check_channel_credentials

    status, _ = _check_channel_credentials("notarealchannel")
    assert status == "unknown"


def test_channel_credentials_telegram_needs_bot_token_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TELEGRAM_BOT_TOKEN required, ADMIN_CHAT_ID optional."""
    from opencomputer.cli_channels import _check_channel_credentials

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.delenv("TELEGRAM_ADMIN_CHAT_ID", raising=False)
    status, _ = _check_channel_credentials("telegram")
    assert status == "paired"


# ─── oc channels status CLI ──────────────────────────────────────────


def test_oc_channels_status_renders_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    from typer.testing import CliRunner

    from opencomputer.cli import app

    r = CliRunner().invoke(app, ["channels", "status"])
    assert r.exit_code == 0, r.output
    flat = " ".join(r.output.split())
    # Discord should appear (channel header expected)
    assert "discord" in flat
    # And status keywords
    assert "missing" in flat or "paired" in flat or "config-driven" in flat


# ─── _check_fd_limit_and_competitors ─────────────────────────────────


def test_fd_check_returns_check_with_status() -> None:
    """Doctor row returns a Check (skip / pass / warn)."""
    from opencomputer.doctor import Check, _check_fd_limit_and_competitors

    result = _check_fd_limit_and_competitors()
    assert isinstance(result, Check)
    assert result.name == "fd limit + competitors"
    assert result.status in {"pass", "warn", "skip"}


def test_fd_check_handles_psutil_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without psutil → skip with informative detail."""
    import sys

    from opencomputer.doctor import _check_fd_limit_and_competitors

    # Block the psutil import for this call
    monkeypatch.setitem(sys.modules, "psutil", None)
    result = _check_fd_limit_and_competitors()
    assert result.status == "skip"
    assert "psutil" in result.detail.lower()


def test_fd_check_handles_resource_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without resource module → skip with informative detail."""
    from opencomputer import doctor

    def _explode(*_args, **_kwargs):
        raise RuntimeError("simulated platform error")

    # Patch the resource.getrlimit lookup at the call site
    import resource

    monkeypatch.setattr(resource, "getrlimit", _explode)
    result = doctor._check_fd_limit_and_competitors()
    assert result.status == "skip"
    assert "resource module" in result.detail.lower()


# ─── Doctor wires the new check ──────────────────────────────────────


def test_doctor_run_includes_fd_check(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """``oc doctor`` invocation returns Check rows including the new fd row."""
    # Just verify the function is exported + callable; the full run_doctor
    # path touches many subsystems. Source-grep is sufficient regression
    # protection (matches the wire-in-audit pattern from PR #576).
    import inspect

    from opencomputer import doctor
    from opencomputer.doctor import _check_fd_limit_and_competitors

    src = inspect.getsource(doctor.run_doctor)
    assert "_check_fd_limit_and_competitors" in src, (
        "run_doctor must call _check_fd_limit_and_competitors"
    )
    # Sanity: function exists
    assert callable(_check_fd_limit_and_competitors)
