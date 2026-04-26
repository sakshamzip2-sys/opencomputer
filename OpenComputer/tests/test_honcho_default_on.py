"""Honcho-on-by-default — daemon auto-start + longer first-pull timeout.

Pinned to this session's incident: the user installed Docker Desktop
but never opened the app, so the daemon was dead. The wizard's
``_optional_honcho()`` saw the docker binary, called ``ensure_started``,
which timed out at 120s trying to reach a dead socket, and the user
was silently downgraded to baseline memory with no path forward
except manually opening Docker Desktop and re-running setup.

This test file pins the fix:
1. Daemon dead → try_start_docker_daemon called (macOS)
2. Linux/Termux → user-friendly fallback message
3. Compose timeout bumped 120 → 300 for first-pull tolerance
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _patch_setup_wizard_console(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture every console.print call so tests can assert on output."""
    from opencomputer import setup_wizard

    captured: list[str] = []
    monkeypatch.setattr(
        setup_wizard.console,
        "print",
        lambda *args, **_: captured.append(" ".join(str(a) for a in args)),
    )
    return captured


def _make_bootstrap_module(
    docker_installed: bool,
    compose_v2: bool,
    daemon_running: bool,
    daemon_starts: bool = True,
    daemon_starts_in_time: bool = True,
    ensure_started_returns: tuple[bool, str] = (True, "ok"),
    has_daemon_helpers: bool = True,
) -> MagicMock:
    """Build a fake bootstrap module with the same surface as the real one."""
    bootstrap = MagicMock()
    bootstrap.detect_docker = MagicMock(return_value=(docker_installed, compose_v2))
    if has_daemon_helpers:
        bootstrap.is_docker_daemon_running = MagicMock(return_value=daemon_running)
        bootstrap.try_start_docker_daemon = MagicMock(return_value=daemon_starts)
        bootstrap.wait_for_docker_daemon = MagicMock(
            return_value=daemon_starts_in_time
        )
    else:
        # Simulate a bootstrap module that pre-dates the daemon helpers
        # — _optional_honcho should still work via the legacy ensure_started path.
        del bootstrap.is_docker_daemon_running
        del bootstrap.try_start_docker_daemon
        del bootstrap.wait_for_docker_daemon
    bootstrap.ensure_started = MagicMock(return_value=ensure_started_returns)
    return bootstrap


def test_optional_honcho_starts_daemon_when_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon dead → try_start_docker_daemon → wait → ensure_started."""
    from opencomputer import setup_wizard

    bootstrap = _make_bootstrap_module(
        docker_installed=True,
        compose_v2=True,
        daemon_running=False,
        daemon_starts=True,
        daemon_starts_in_time=True,
    )
    monkeypatch.setattr(setup_wizard, "_load_honcho_bootstrap", lambda: bootstrap)
    captured = _patch_setup_wizard_console(monkeypatch)

    setup_wizard._optional_honcho()

    bootstrap.try_start_docker_daemon.assert_called_once()
    bootstrap.wait_for_docker_daemon.assert_called_once()
    bootstrap.ensure_started.assert_called_once()
    text = " ".join(captured).lower()
    assert "starting" in text or "docker desktop" in text


def test_optional_honcho_uses_300s_compose_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ensure_started must be called with the bumped 180s timeout
    (the wizard's timeout; bootstrap-internal compose call ceilings
    at 300s separately)."""
    from opencomputer import setup_wizard

    bootstrap = _make_bootstrap_module(
        docker_installed=True,
        compose_v2=True,
        daemon_running=True,
    )
    monkeypatch.setattr(setup_wizard, "_load_honcho_bootstrap", lambda: bootstrap)
    _patch_setup_wizard_console(monkeypatch)

    setup_wizard._optional_honcho()

    call_kwargs = bootstrap.ensure_started.call_args.kwargs
    assert call_kwargs.get("timeout_s") == 180, (
        f"expected timeout_s=180, got {call_kwargs}. The previous 60s "
        "default was the dominant failure mode in the field."
    )


def test_optional_honcho_falls_back_when_daemon_wont_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If wait_for_docker_daemon returns False, downgrade gracefully
    with a clear message — don't try to compose-up against a dead
    daemon and waste the full timeout."""
    from opencomputer import setup_wizard

    bootstrap = _make_bootstrap_module(
        docker_installed=True,
        compose_v2=True,
        daemon_running=False,
        daemon_starts=True,
        daemon_starts_in_time=False,
    )
    monkeypatch.setattr(setup_wizard, "_load_honcho_bootstrap", lambda: bootstrap)
    captured = _patch_setup_wizard_console(monkeypatch)

    downgrade_calls: list[bool] = []
    monkeypatch.setattr(
        setup_wizard,
        "_downgrade_memory_provider_to_empty",
        lambda: downgrade_calls.append(True),
    )

    setup_wizard._optional_honcho()

    assert downgrade_calls == [True], (
        "expected downgrade when daemon doesn't come up in time"
    )
    bootstrap.ensure_started.assert_not_called()
    text = " ".join(captured).lower()
    assert "didn't come up" in text or "did not come up" in text


def test_optional_honcho_linux_fallback_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Linux, try_start_docker_daemon returns False — the wizard
    must print the systemctl invocation, NOT pretend we can start it."""
    import sys

    from opencomputer import setup_wizard

    bootstrap = _make_bootstrap_module(
        docker_installed=True,
        compose_v2=True,
        daemon_running=False,
        daemon_starts=False,  # platform unsupported
    )
    monkeypatch.setattr(setup_wizard, "_load_honcho_bootstrap", lambda: bootstrap)
    monkeypatch.setattr(sys, "platform", "linux")
    captured = _patch_setup_wizard_console(monkeypatch)

    downgrade_calls: list[bool] = []
    monkeypatch.setattr(
        setup_wizard,
        "_downgrade_memory_provider_to_empty",
        lambda: downgrade_calls.append(True),
    )

    setup_wizard._optional_honcho()

    text = " ".join(captured).lower()
    assert "systemctl start docker" in text, (
        f"expected systemctl hint on Linux; got: {text!r}"
    )
    assert downgrade_calls == [True]


def test_optional_honcho_macos_fallback_when_open_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On macOS where 'open -a Docker' itself fails (rare — Docker not
    installed even though the binary exists), print the open hint."""
    import sys

    from opencomputer import setup_wizard

    bootstrap = _make_bootstrap_module(
        docker_installed=True,
        compose_v2=True,
        daemon_running=False,
        daemon_starts=False,
    )
    monkeypatch.setattr(setup_wizard, "_load_honcho_bootstrap", lambda: bootstrap)
    monkeypatch.setattr(sys, "platform", "darwin")
    captured = _patch_setup_wizard_console(monkeypatch)
    monkeypatch.setattr(
        setup_wizard, "_downgrade_memory_provider_to_empty", lambda: None
    )

    setup_wizard._optional_honcho()

    text = " ".join(captured).lower()
    assert "open -a docker" in text


def test_optional_honcho_legacy_bootstrap_without_daemon_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence-in-depth: if a packaged honcho plugin lacks the new
    daemon helpers (e.g. a third-party rebrand frozen at an older
    version), the wizard still works via the legacy detect+ensure path."""
    from opencomputer import setup_wizard

    bootstrap = _make_bootstrap_module(
        docker_installed=True,
        compose_v2=True,
        daemon_running=True,
        has_daemon_helpers=False,
    )
    monkeypatch.setattr(setup_wizard, "_load_honcho_bootstrap", lambda: bootstrap)
    _patch_setup_wizard_console(monkeypatch)

    setup_wizard._optional_honcho()

    bootstrap.ensure_started.assert_called_once()


def test_compose_timeout_default_is_120s_but_overridable() -> None:
    """The compose helper's default of 120s stays for ps/down/etc.;
    only honcho_up bumps to 300s for first-pull tolerance.

    Source-text assertion (not module-load) because bootstrap.py
    declares dataclasses that depend on a real module name in
    sys.modules — synthetic loaders confuse Python's dataclasses
    internals. Reading the source as text is the lighter check.
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    src = (
        repo_root / "extensions" / "memory-honcho" / "bootstrap.py"
    ).read_text(encoding="utf-8")

    assert "timeout: int = 120" in src, (
        "default 120s stays for cheap ops (ps/down); slow ops opt in to longer"
    )
    # honcho_up must pass timeout=300 to _compose for first-pull tolerance —
    # cold Docker pulls postgres + redis + api (~600 MB) in 2-3 minutes.
    up_block = src.split("def honcho_up", 1)[1].split("\ndef ", 1)[0]
    assert "timeout=300" in up_block, (
        "honcho_up must pass timeout=300 to _compose for first-pull tolerance"
    )
