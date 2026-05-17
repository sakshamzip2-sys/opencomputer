"""Tests for the unified ``oc gateway *`` Typer group (Task 1.8).

Plan: docs/superpowers/plans/2026-05-08-messaging-gateway-parity.md (T1.8)
Spec: docs/superpowers/specs/2026-05-08-messaging-gateway-parity-design.md (§5.1)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from opencomputer.cli_gateway import gateway_app, pairing_app, top_pairing_app

runner = CliRunner()


# ── Group structure ────────────────────────────────────────────────────────


def test_gateway_help_lists_all_subcommands():
    result = runner.invoke(gateway_app, ["--help"])
    assert result.exit_code == 0
    expected = {
        "run",
        "setup",
        "install",
        "uninstall",
        "start",
        "stop",
        "restart",
        "status",
        "logs",
        "sethome",
        "pairing",
    }
    for cmd in expected:
        assert cmd in result.output


def test_pairing_help_lists_subcommands():
    result = runner.invoke(pairing_app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("list", "approve", "approve-deeplink", "revoke", "regen", "clear-pending"):
        assert cmd in result.output


def test_top_pairing_alias_exists():
    """Hermes-CLI compat: `oc pairing list` mirrors `oc gateway pairing list`."""
    result = runner.invoke(top_pairing_app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("list", "approve", "revoke", "regen"):
        assert cmd in result.output


# ── Bare invocation runs foreground ────────────────────────────────────────


def test_bare_gateway_invokes_run_foreground():
    """Bare `oc gateway` (no subcommand) → calls _run_foreground."""
    with patch("opencomputer.cli_gateway._run_foreground") as mock_run:
        result = runner.invoke(gateway_app, [])
        assert result.exit_code == 0
        mock_run.assert_called_once()


def test_install_daemon_flag_deprecated_routes_to_install():
    """`oc gateway --install-daemon` (deprecated) routes to install + warns."""
    with patch("opencomputer.cli_gateway._install_service") as mock_install:
        result = runner.invoke(
            gateway_app,
            ["--install-daemon", "--daemon-profile", "work"],
        )
        # Exit 0 because we explicitly raise typer.Exit(0).
        assert result.exit_code == 0
        mock_install.assert_called_once_with(profile="work", system=False)


def test_run_subcommand_invokes_foreground():
    with patch("opencomputer.cli_gateway._run_foreground") as mock_run:
        result = runner.invoke(gateway_app, ["run"])
        assert result.exit_code == 0
        mock_run.assert_called_once()


# ── Service lifecycle ──────────────────────────────────────────────────────


def test_install_calls_backend(monkeypatch):
    install_called = {}

    class FakeResult:
        backend = "systemd-user"
        config_path = Path("/tmp/foo")
        notes: list[str] = []

    class FakeBackend:
        def install(self, profile, extra_args):
            install_called["profile"] = profile
            install_called["extra_args"] = extra_args
            return FakeResult()

    monkeypatch.setattr(
        "opencomputer.service.factory.get_backend", lambda: FakeBackend()
    )
    result = runner.invoke(gateway_app, ["install", "--profile", "work"])
    assert result.exit_code == 0
    assert install_called["profile"] == "work"
    assert install_called["extra_args"] == "gateway"


def test_uninstall_calls_backend_uninstall_with_profile_kwarg(monkeypatch):
    """``ServiceBackend.uninstall`` is profile-aware (mirrors ``install``):
    it takes ``*, profile``. ``oc gateway uninstall --profile X`` must
    forward ``profile=X`` so the backend removes X's service unit, not
    the default profile's.

    This FakeBackend mirrors the real Protocol: ``uninstall`` accepts a
    keyword-only ``profile``. The pre-fix call ``backend.uninstall()``
    (no args) cannot pass the user's chosen profile through.
    """
    from opencomputer.service.base import UninstallResult

    uninstall_called = {}

    class FakeBackend:
        def uninstall(self, *, profile: str) -> UninstallResult:
            uninstall_called["profile"] = profile
            return UninstallResult(
                backend="systemd-user",
                file_removed=True,
                config_path=Path("/tmp/foo.service"),
                notes=[],
            )

    monkeypatch.setattr(
        "opencomputer.service.factory.get_backend", lambda: FakeBackend()
    )
    result = runner.invoke(gateway_app, ["uninstall", "--profile", "work"])
    assert result.exit_code == 0, (
        f"oc gateway uninstall failed (exit {result.exit_code}); "
        f"output={result.output!r}"
    )
    # The user's --profile must reach the backend, not the default.
    assert uninstall_called.get("profile") == "work"
    assert result.exception is None or not isinstance(
        result.exception, TypeError
    ), f"uninstall raised TypeError: {result.exception!r}"


def test_start_calls_backend_start(monkeypatch):
    class FakeBackend:
        def start(self):
            return True

    monkeypatch.setattr(
        "opencomputer.service.factory.get_backend", lambda: FakeBackend()
    )
    result = runner.invoke(gateway_app, ["start"])
    assert result.exit_code == 0
    assert "started" in result.output


def test_stop_calls_backend_stop(monkeypatch):
    class FakeBackend:
        def stop(self):
            return True

    monkeypatch.setattr(
        "opencomputer.service.factory.get_backend", lambda: FakeBackend()
    )
    result = runner.invoke(gateway_app, ["stop"])
    assert result.exit_code == 0
    assert "stopped" in result.output


def test_restart_invokes_stop_then_start(monkeypatch, tmp_path):
    sequence = []

    class FakeBackend:
        def stop(self):
            sequence.append("stop")
            return True

        def start(self):
            sequence.append("start")
            return True

    monkeypatch.setattr(
        "opencomputer.service.factory.get_backend", lambda: FakeBackend()
    )
    monkeypatch.setattr(
        "opencomputer.cli_gateway._profile_home", lambda: tmp_path
    )
    result = runner.invoke(gateway_app, ["restart", "--drain-timeout", "5"])
    assert result.exit_code == 0
    assert sequence == ["stop", "start"]
    # Drain flag was written.
    assert (tmp_path / "gateway" / "drain.flag").exists()


# ── Status (uses cli_gateway_status) ──────────────────────────────────────


def test_status_renders_snapshot(monkeypatch):
    """Status command should call `get_gateway_runtime_snapshot` + render."""
    from opencomputer.cli_gateway_status import (
        GatewayRuntimeSnapshot,
    )

    fake = GatewayRuntimeSnapshot(
        manager="systemd-user",
        service_installed=True,
        service_running=True,
        main_pid=12345,
    )
    monkeypatch.setattr(
        "opencomputer.cli_gateway_status.get_gateway_runtime_snapshot",
        lambda profile="default": fake,
    )
    result = runner.invoke(gateway_app, ["status"])
    assert result.exit_code == 0
    # Output should mention manager + PID.
    assert "systemd-user" in result.output
    assert "12345" in result.output


# ── sethome ────────────────────────────────────────────────────────────────


def test_sethome_writes_home_channels_json(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "opencomputer.cli_gateway._profile_home", lambda: tmp_path
    )
    result = runner.invoke(
        gateway_app, ["sethome", "telegram", "123456789"]
    )
    assert result.exit_code == 0
    home_path = tmp_path / "gateway" / "home_channels.json"
    assert home_path.exists()
    data = json.loads(home_path.read_text(encoding="utf-8"))
    assert data["telegram"] == "123456789"


def test_sethome_with_thread_appends_id(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "opencomputer.cli_gateway._profile_home", lambda: tmp_path
    )
    result = runner.invoke(
        gateway_app,
        ["sethome", "discord", "999", "--thread", "T123"],
    )
    assert result.exit_code == 0
    data = json.loads(
        (tmp_path / "gateway" / "home_channels.json").read_text(encoding="utf-8")
    )
    assert data["discord"] == "999:T123"


def test_sethome_list_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "opencomputer.cli_gateway._profile_home", lambda: tmp_path
    )
    result = runner.invoke(gateway_app, ["sethome", "--list"])
    assert result.exit_code == 0
    assert "no home" in result.output.lower()


def test_sethome_clear_removes_entry(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "opencomputer.cli_gateway._profile_home", lambda: tmp_path
    )
    runner.invoke(gateway_app, ["sethome", "telegram", "123"])
    runner.invoke(gateway_app, ["sethome", "discord", "456"])
    result = runner.invoke(gateway_app, ["sethome", "--clear", "telegram"])
    assert result.exit_code == 0
    data = json.loads(
        (tmp_path / "gateway" / "home_channels.json").read_text(encoding="utf-8")
    )
    assert "telegram" not in data
    assert data["discord"] == "456"


# ── Pairing subgroup ───────────────────────────────────────────────────────


def test_pairing_list_empty_state(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "opencomputer.cli_gateway._profile_home", lambda: tmp_path
    )
    result = runner.invoke(pairing_app, ["list"])
    assert result.exit_code == 0
    assert "no pending" in result.output.lower()


def test_pairing_approve_happy_path(monkeypatch, tmp_path):
    from opencomputer.channels.pairing_codes import PairingCodeStore

    monkeypatch.setattr(
        "opencomputer.cli_gateway._profile_home", lambda: tmp_path
    )
    # Pre-mint a code so approve can land.
    store = PairingCodeStore(tmp_path)
    code = store.generate_code("telegram", "user-x", "Alice")
    assert code is not None

    result = runner.invoke(pairing_app, ["approve", "telegram", code])
    assert result.exit_code == 0
    assert "approved" in result.output.lower()


def test_pairing_approve_unknown_code_exits_1(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "opencomputer.cli_gateway._profile_home", lambda: tmp_path
    )
    result = runner.invoke(pairing_app, ["approve", "telegram", "BADCODE0"])
    assert result.exit_code == 1


def test_pairing_revoke(monkeypatch, tmp_path):
    from opencomputer.channels.pairing_codes import PairingCodeStore

    monkeypatch.setattr(
        "opencomputer.cli_gateway._profile_home", lambda: tmp_path
    )
    store = PairingCodeStore(tmp_path)
    code = store.generate_code("telegram", "user-y", "")
    store.approve_code("telegram", code)
    result = runner.invoke(pairing_app, ["revoke", "telegram", "user-y"])
    assert result.exit_code == 0


def test_pairing_regen_force_mints(monkeypatch, tmp_path):
    from opencomputer.channels.pairing_codes import PairingCodeStore

    monkeypatch.setattr(
        "opencomputer.cli_gateway._profile_home", lambda: tmp_path
    )
    store = PairingCodeStore(tmp_path)
    store.generate_code("telegram", "user-z")
    # Standard generate is rate-limited, but regen bypasses.
    result = runner.invoke(pairing_app, ["regen", "telegram", "user-z"])
    assert result.exit_code == 0


def test_pairing_clear_pending(monkeypatch, tmp_path):
    from opencomputer.channels.pairing_codes import PairingCodeStore

    monkeypatch.setattr(
        "opencomputer.cli_gateway._profile_home", lambda: tmp_path
    )
    store = PairingCodeStore(tmp_path)
    store.generate_code("telegram", "u1")
    store.generate_code("telegram", "u2")
    result = runner.invoke(pairing_app, ["clear-pending", "telegram"])
    assert result.exit_code == 0
    assert "cleared 2" in result.output.lower()


def test_pairing_approve_deeplink_parses_url(monkeypatch, tmp_path):
    from opencomputer.channels.pairing_codes import PairingCodeStore

    monkeypatch.setattr(
        "opencomputer.cli_gateway._profile_home", lambda: tmp_path
    )
    store = PairingCodeStore(tmp_path)
    code = store.generate_code("telegram", "dl-user", "")
    result = runner.invoke(
        pairing_app,
        ["approve-deeplink", f"https://t.me/MyBot?start=approve_{code}"],
    )
    assert result.exit_code == 0


def test_pairing_approve_deeplink_invalid_url(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "opencomputer.cli_gateway._profile_home", lambda: tmp_path
    )
    result = runner.invoke(
        pairing_app, ["approve-deeplink", "https://not-a-deeplink/x"]
    )
    assert result.exit_code == 1


# ── Hermes-compat top-level alias ─────────────────────────────────────────


def test_top_pairing_list_works(monkeypatch, tmp_path):
    """`oc pairing list` (top-level Hermes-compat alias) works."""
    monkeypatch.setattr(
        "opencomputer.cli_gateway._profile_home", lambda: tmp_path
    )
    result = runner.invoke(top_pairing_app, ["list"])
    assert result.exit_code == 0
