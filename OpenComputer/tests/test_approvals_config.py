"""Tests for security.approvals.{mode,timeout} config (Hermes parity)."""
from __future__ import annotations

import pytest

from opencomputer.security.approvals import (
    DEFAULT_MODE,
    DEFAULT_TIMEOUT_S,
    ApprovalsConfig,
    load_approvals_from_active_config,
    parse_mode,
    parse_timeout,
)


# ── parse_mode ────────────────────────────────────────────────────────


def test_parse_mode_manual():
    assert parse_mode("manual") == "manual"


def test_parse_mode_off():
    assert parse_mode("off") == "off"


def test_parse_mode_smart_falls_back_to_manual(caplog):
    """Smart mode is recognised but not yet implemented — falls back."""
    with caplog.at_level("WARNING", logger="opencomputer.security.approvals"):
        assert parse_mode("smart") == "manual"
    assert "smart" in caplog.text.lower()


def test_parse_mode_unknown_logs_and_falls_back(caplog):
    with caplog.at_level("WARNING", logger="opencomputer.security.approvals"):
        assert parse_mode("YOLO_MODE_PLEASE") == DEFAULT_MODE
    assert "unknown" in caplog.text.lower()


def test_parse_mode_none_returns_default():
    assert parse_mode(None) == DEFAULT_MODE


def test_parse_mode_strips_whitespace_lowercases():
    assert parse_mode("  Manual  ") == "manual"


# ── parse_timeout ─────────────────────────────────────────────────────


def test_parse_timeout_int():
    assert parse_timeout(60) == 60.0


def test_parse_timeout_float_string():
    assert parse_timeout("45.5") == 45.5


def test_parse_timeout_negative_clamps_to_one():
    assert parse_timeout(-5) == 1.0
    assert parse_timeout(0) == 1.0


def test_parse_timeout_invalid_returns_default():
    assert parse_timeout("not-a-number") == DEFAULT_TIMEOUT_S
    assert parse_timeout(None) == DEFAULT_TIMEOUT_S


# ── ApprovalsConfig.auto_allow ────────────────────────────────────────


def test_auto_allow_when_off():
    cfg = ApprovalsConfig(mode="off")
    assert cfg.auto_allow is True


def test_auto_allow_false_when_manual():
    cfg = ApprovalsConfig(mode="manual")
    assert cfg.auto_allow is False


# ── load_approvals_from_active_config ─────────────────────────────────


def test_load_returns_defaults_when_no_section(monkeypatch, tmp_path):
    cfg_dir = tmp_path
    (cfg_dir / "config.yaml").write_text("agent:\n  loop_budget: 100\n")

    monkeypatch.setattr(
        "opencomputer.profiles.read_active_profile", lambda: "default",
    )
    monkeypatch.setattr(
        "opencomputer.profiles.profile_home_dir", lambda _name: cfg_dir,
    )
    cfg = load_approvals_from_active_config()
    assert cfg.mode == DEFAULT_MODE
    assert cfg.timeout_s == DEFAULT_TIMEOUT_S


def test_load_reads_section(monkeypatch, tmp_path):
    cfg_dir = tmp_path
    (cfg_dir / "config.yaml").write_text(
        "security:\n  approvals:\n    mode: off\n    timeout: 45\n"
    )

    monkeypatch.setattr(
        "opencomputer.profiles.read_active_profile", lambda: "default",
    )
    monkeypatch.setattr(
        "opencomputer.profiles.profile_home_dir", lambda _name: cfg_dir,
    )
    cfg = load_approvals_from_active_config()
    assert cfg.mode == "off"
    assert cfg.timeout_s == 45.0
    assert cfg.auto_allow is True


def test_load_handles_corrupt_yaml(monkeypatch, tmp_path):
    cfg_dir = tmp_path
    (cfg_dir / "config.yaml").write_text("security: {approvals: }invalid")

    monkeypatch.setattr(
        "opencomputer.profiles.read_active_profile", lambda: "default",
    )
    monkeypatch.setattr(
        "opencomputer.profiles.profile_home_dir", lambda _name: cfg_dir,
    )
    cfg = load_approvals_from_active_config()
    # Corrupt YAML → defaults.
    assert cfg.mode == DEFAULT_MODE
    assert cfg.timeout_s == DEFAULT_TIMEOUT_S
