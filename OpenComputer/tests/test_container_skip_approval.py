"""Tests for container-skip-approval (Hermes parity)."""
from __future__ import annotations

import pytest

from opencomputer.tools.bash_safety import (
    detect_destructive_with_context,
    is_sandbox_strategy_container_isolated,
    load_active_sandbox_strategy,
)

# ── strategy classifier ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "strategy",
    ["docker", "singularity", "modal", "daytona", "vercel_sandbox", "DOCKER"],
)
def test_container_isolated_strategies(strategy: str):
    assert is_sandbox_strategy_container_isolated(strategy) is True


@pytest.mark.parametrize(
    "strategy", ["local", "ssh", "linux", "macos", "none", None, ""],
)
def test_non_container_strategies(strategy):
    assert is_sandbox_strategy_container_isolated(strategy) is False


# ── load_active_sandbox_strategy ─────────────────────────────────────


def test_load_strategy_from_config(monkeypatch, tmp_path):
    cfg_dir = tmp_path
    (cfg_dir / "config.yaml").write_text("sandbox:\n  strategy: docker\n")

    monkeypatch.setattr(
        "opencomputer.profiles.read_active_profile", lambda: "default",
    )
    monkeypatch.setattr(
        "opencomputer.profiles.profile_home_dir", lambda _name: cfg_dir,
    )
    assert load_active_sandbox_strategy() == "docker"


def test_load_strategy_returns_none_on_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "opencomputer.profiles.read_active_profile", lambda: "default",
    )
    monkeypatch.setattr(
        "opencomputer.profiles.profile_home_dir", lambda _name: tmp_path,
    )
    assert load_active_sandbox_strategy() is None


def test_load_strategy_handles_corrupt_yaml(monkeypatch, tmp_path):
    cfg_dir = tmp_path
    (cfg_dir / "config.yaml").write_text("sandbox: }invalid")

    monkeypatch.setattr(
        "opencomputer.profiles.read_active_profile", lambda: "default",
    )
    monkeypatch.setattr(
        "opencomputer.profiles.profile_home_dir", lambda _name: cfg_dir,
    )
    assert load_active_sandbox_strategy() is None


# ── detect_destructive_with_context ──────────────────────────────────


def test_context_suppresses_when_docker(monkeypatch, tmp_path):
    cfg_dir = tmp_path
    (cfg_dir / "config.yaml").write_text("sandbox:\n  strategy: docker\n")

    monkeypatch.setattr(
        "opencomputer.profiles.read_active_profile", lambda: "default",
    )
    monkeypatch.setattr(
        "opencomputer.profiles.profile_home_dir", lambda _name: cfg_dir,
    )
    # Real destructive command — but sandbox is docker, so suppressed.
    assert detect_destructive_with_context("rm -rf /tmp/foo") is None
    assert detect_destructive_with_context("systemctl stop sshd") is None


def test_context_does_not_suppress_when_local(monkeypatch, tmp_path):
    cfg_dir = tmp_path
    (cfg_dir / "config.yaml").write_text("sandbox:\n  strategy: local\n")

    monkeypatch.setattr(
        "opencomputer.profiles.read_active_profile", lambda: "default",
    )
    monkeypatch.setattr(
        "opencomputer.profiles.profile_home_dir", lambda _name: cfg_dir,
    )
    # No sandbox isolation — must still detect.
    hit = detect_destructive_with_context("systemctl stop sshd")
    assert hit is not None
    assert hit.pattern_id == "systemctl_disrupt"


def test_context_respects_command_allowlist(monkeypatch, tmp_path):
    cfg_dir = tmp_path
    (cfg_dir / "config.yaml").write_text(
        "sandbox:\n  strategy: local\n"
        "security:\n  command_allowlist:\n    - systemctl\n"
    )

    monkeypatch.setattr(
        "opencomputer.profiles.read_active_profile", lambda: "default",
    )
    monkeypatch.setattr(
        "opencomputer.profiles.profile_home_dir", lambda _name: cfg_dir,
    )
    # No container isolation, but user allowlisted systemctl.
    assert detect_destructive_with_context("systemctl stop sshd") is None


def test_context_does_not_suppress_other_destructive_when_allowlisted(
    monkeypatch, tmp_path,
):
    """Allowlisting `systemctl` should NOT also suppress `rm -rf /`."""
    cfg_dir = tmp_path
    (cfg_dir / "config.yaml").write_text(
        "sandbox:\n  strategy: local\n"
        "security:\n  command_allowlist:\n    - systemctl\n"
    )

    monkeypatch.setattr(
        "opencomputer.profiles.read_active_profile", lambda: "default",
    )
    monkeypatch.setattr(
        "opencomputer.profiles.profile_home_dir", lambda _name: cfg_dir,
    )
    assert detect_destructive_with_context("rm -rf /") is not None
