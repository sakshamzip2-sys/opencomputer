"""Tests for the bg-notify filter (Task B3).

Plan: docs/superpowers/plans/2026-05-08-messaging-gateway-parity.md (T2.3)

The filter sits inside ``opencomputer.agent.bg_notify._should_emit``,
gating the default Notification subscriber via the
``display.background_process_notifications`` knob.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from opencomputer.agent.bg_notify import (
    BgProcessExit,
    _resolve_bg_notify_mode,
    _should_emit,
)


def _payload(exit_code: int = 0) -> BgProcessExit:
    return BgProcessExit(
        session_id="s",
        tool_call_id="t",
        exit_code=exit_code,
        tail_stdout="",
        tail_stderr="",
        duration_seconds=0.0,
    )


# ── _should_emit modes ─────────────────────────────────────────────────────


def test_off_suppresses_all(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_BACKGROUND_NOTIFICATIONS", "off")
    assert _should_emit(_payload(0)) is False
    assert _should_emit(_payload(1)) is False


def test_all_emits_everything(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_BACKGROUND_NOTIFICATIONS", "all")
    assert _should_emit(_payload(0)) is True
    assert _should_emit(_payload(1)) is True


def test_result_emits_on_completion(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_BACKGROUND_NOTIFICATIONS", "result")
    # "result" emits regardless of exit code (same as "all" for the
    # process-exit handler — both fire on completion).
    assert _should_emit(_payload(0)) is True
    assert _should_emit(_payload(1)) is True


def test_error_emits_only_on_nonzero(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_BACKGROUND_NOTIFICATIONS", "error")
    assert _should_emit(_payload(0)) is False
    assert _should_emit(_payload(1)) is True
    assert _should_emit(_payload(-1)) is True


def test_unknown_mode_fails_open(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_BACKGROUND_NOTIFICATIONS", "garbage")
    # Garbage env value isn't recognised → resolve falls back to config →
    # default "all" → emit.
    assert _should_emit(_payload(0)) is True


# ── Resolution: env beats config ───────────────────────────────────────────


def test_env_overrides_config(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_BACKGROUND_NOTIFICATIONS", "off")
    # Even if config says "all", env wins.
    with patch(
        "opencomputer.agent.bg_notify.config_file_path", create=True
    ):
        assert _should_emit(_payload(0)) is False


# ── Resolution: per-platform via display_config ────────────────────────────


def test_per_platform_resolution(monkeypatch, tmp_path):
    """Per-platform overrides come through resolve_display_setting."""
    monkeypatch.delenv("OPENCOMPUTER_BACKGROUND_NOTIFICATIONS", raising=False)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
display:
  background_process_notifications: all
  platforms:
    slack:
      background_process_notifications: error
""",
        encoding="utf-8",
    )

    from opencomputer.agent import bg_notify as bn

    monkeypatch.setattr(
        bn, "_resolve_bg_notify_mode", lambda platform_key=None: "error" if platform_key == "slack" else "all"
    )
    # Test the wired _should_emit using per-platform key.
    assert bn._should_emit(_payload(exit_code=0), platform_key="slack") is False
    assert bn._should_emit(_payload(exit_code=1), platform_key="slack") is True
    assert bn._should_emit(_payload(exit_code=0), platform_key="telegram") is True


def test_resolve_mode_default_when_no_config(monkeypatch):
    """When no env override and no readable config — default is "all"."""
    monkeypatch.delenv("OPENCOMPUTER_BACKGROUND_NOTIFICATIONS", raising=False)
    # Config-load failure path returns "all" (built-in default).
    with patch(
        "opencomputer.agent.bg_notify.config_file_path", create=True
    ) as mock_path:
        mock_path.side_effect = Exception("no config")
        mode = _resolve_bg_notify_mode()
    assert mode == "all"
