"""Tests for the Hermes-parity command_allowlist suppression."""
from __future__ import annotations

from opencomputer.tools.bash_safety import (
    detect_destructive,
    detect_destructive_with_allowlist,
    is_command_allowlisted,
    load_command_allowlist_from_active_config,
)

# ── leading-token matcher ────────────────────────────────────────────


def test_allowlist_matches_leading_token():
    assert is_command_allowlisted("rm -rf /tmp/foo", ["rm"]) is True
    assert is_command_allowlisted("systemctl stop sshd", ["systemctl"]) is True


def test_allowlist_does_not_match_unrelated():
    assert is_command_allowlisted("ls -la", ["rm", "systemctl"]) is False


def test_allowlist_handles_full_path_command():
    assert is_command_allowlisted("/usr/bin/rm -rf /tmp/foo", ["rm"]) is True


def test_allowlist_pattern_id_match():
    """Power users can pin a specific pattern_id."""
    assert (
        is_command_allowlisted("chmod 666 /tmp/foo", ["chmod_666"]) is True
    )


def test_empty_allowlist_never_matches():
    assert is_command_allowlisted("rm -rf /", []) is False
    assert is_command_allowlisted("rm -rf /", ()) is False


def test_empty_command_never_matches():
    assert is_command_allowlisted("", ["rm"]) is False
    assert is_command_allowlisted("   ", ["rm"]) is False


# ── detect_destructive_with_allowlist ───────────────────────────────


def test_with_allowlist_suppresses_advisory_match():
    cmd = "rm -rf /tmp/foo"
    # baseline — without allowlist this would NOT match (rm -rf /tmp/foo
    # is not a root-ish target). Use a real match instead:
    cmd2 = "systemctl stop sshd"
    assert detect_destructive(cmd2) is not None
    # With "systemctl" allowlisted, suppression kicks in.
    assert detect_destructive_with_allowlist(cmd2, ["systemctl"]) is None


def test_with_allowlist_does_not_suppress_other_matches():
    """Allowlisting `systemctl` must not suppress an `rm -rf /` detection."""
    assert detect_destructive_with_allowlist("rm -rf /", ["systemctl"]) is not None


def test_with_allowlist_handles_none():
    assert detect_destructive_with_allowlist("rm -rf /", None) is not None


def test_with_allowlist_handles_empty():
    assert detect_destructive_with_allowlist("rm -rf /", []) is not None


# ── load_command_allowlist_from_active_config ───────────────────────


def test_load_from_config_returns_empty_when_no_section(monkeypatch, tmp_path):
    cfg_dir = tmp_path
    (cfg_dir / "config.yaml").write_text("agent:\n  loop_budget: 100\n")

    monkeypatch.setattr(
        "opencomputer.profiles.read_active_profile", lambda: "default",
    )
    monkeypatch.setattr(
        "opencomputer.profiles.profile_home_dir", lambda _name: cfg_dir,
    )
    assert load_command_allowlist_from_active_config() == ()


def test_load_from_config_returns_entries(monkeypatch, tmp_path):
    cfg_dir = tmp_path
    (cfg_dir / "config.yaml").write_text(
        "security:\n"
        "  command_allowlist:\n"
        "    - rm\n"
        "    - systemctl\n"
        "    - chmod_666\n"
    )

    monkeypatch.setattr(
        "opencomputer.profiles.read_active_profile", lambda: "default",
    )
    monkeypatch.setattr(
        "opencomputer.profiles.profile_home_dir", lambda _name: cfg_dir,
    )
    out = load_command_allowlist_from_active_config()
    assert out == ("rm", "systemctl", "chmod_666")


def test_load_from_config_handles_corrupt_yaml(monkeypatch, tmp_path):
    cfg_dir = tmp_path
    (cfg_dir / "config.yaml").write_text("security: {command_allowlist: }invalid")

    monkeypatch.setattr(
        "opencomputer.profiles.read_active_profile", lambda: "default",
    )
    monkeypatch.setattr(
        "opencomputer.profiles.profile_home_dir", lambda _name: cfg_dir,
    )
    assert load_command_allowlist_from_active_config() == ()


def test_load_from_config_filters_non_string_entries(monkeypatch, tmp_path):
    cfg_dir = tmp_path
    (cfg_dir / "config.yaml").write_text(
        "security:\n  command_allowlist:\n    - rm\n    - 42\n    - ''\n    - systemctl\n"
    )

    monkeypatch.setattr(
        "opencomputer.profiles.read_active_profile", lambda: "default",
    )
    monkeypatch.setattr(
        "opencomputer.profiles.profile_home_dir", lambda _name: cfg_dir,
    )
    out = load_command_allowlist_from_active_config()
    # Non-string and empty-string entries dropped.
    assert "rm" in out and "systemctl" in out
    assert 42 not in out  # type: ignore[comparison-overlap]
    assert "" not in out
