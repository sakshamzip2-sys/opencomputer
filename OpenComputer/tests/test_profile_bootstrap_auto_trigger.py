"""Auto-trigger for profile bootstrap on first chat (user vision).

User said verbatim: "the chat llm should know about the user before
the user even starts using it". PR #143 shipped the bootstrap
orchestrator but as a manual CLI invocation. This module fires it
automatically on first chat in a background daemon thread (quick mode:
identity + git only, no browser/calendar — those are slow and need
entitlements on macOS Sequoia).
"""
from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the auto-trigger at a throwaway profile dir so tests don't
    touch the developer's real ~/.opencomputer/ marker file."""
    profile_dir = tmp_path / ".opencomputer"
    profile_dir.mkdir()
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(profile_dir))


def _force_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeTTY:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", FakeTTY())


def test_should_auto_bootstrap_returns_true_on_first_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First run on a TTY → policy says yes."""
    from opencomputer.profile_bootstrap.auto_trigger import should_auto_bootstrap

    _force_tty(monkeypatch)
    monkeypatch.delenv("OPENCOMPUTER_NO_AUTO_BOOTSTRAP", raising=False)

    should, reason = should_auto_bootstrap()
    assert should is True
    assert "first-run" in reason or "marker absent" in reason


def test_should_auto_bootstrap_skips_on_existing_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Marker file exists → already bootstrapped, skip."""
    from opencomputer.profile_bootstrap import auto_trigger

    _force_tty(monkeypatch)
    monkeypatch.delenv("OPENCOMPUTER_NO_AUTO_BOOTSTRAP", raising=False)
    marker = auto_trigger._marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"completed_at": 1.0}))

    should, reason = auto_trigger.should_auto_bootstrap()
    assert should is False
    assert "marker" in reason.lower() or "already" in reason.lower()


def test_should_auto_bootstrap_skips_on_non_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CI / piped stdin → don't auto-bootstrap (would be invisible)."""
    from opencomputer.profile_bootstrap.auto_trigger import should_auto_bootstrap

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.delenv("OPENCOMPUTER_NO_AUTO_BOOTSTRAP", raising=False)

    should, reason = should_auto_bootstrap()
    assert should is False
    assert "tty" in reason.lower() or "stdin" in reason.lower()


def test_should_auto_bootstrap_skips_on_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opt-out env var → skip even on TTY first-run."""
    from opencomputer.profile_bootstrap.auto_trigger import should_auto_bootstrap

    _force_tty(monkeypatch)
    monkeypatch.setenv("OPENCOMPUTER_NO_AUTO_BOOTSTRAP", "1")

    should, reason = should_auto_bootstrap()
    assert should is False
    assert "opted out" in reason.lower() or "OPENCOMPUTER_NO_AUTO_BOOTSTRAP" in reason


def test_kick_off_returns_none_when_policy_says_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skip path returns None — caller uses that to decide whether to
    print the 'Building your profile…' notice."""
    from opencomputer.profile_bootstrap.auto_trigger import kick_off_in_background

    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # non-TTY → skip

    assert kick_off_in_background() is None


def test_kick_off_returns_thread_when_policy_says_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run path returns the started Thread; tests can join() it."""
    from opencomputer.profile_bootstrap import auto_trigger

    _force_tty(monkeypatch)
    monkeypatch.delenv("OPENCOMPUTER_NO_AUTO_BOOTSTRAP", raising=False)

    # Stub the orchestrator so the test doesn't try to scan the real
    # ~/Documents and write to the real graph DB.
    ran: list[bool] = []

    def fake_run_bootstrap(**kwargs):
        ran.append(True)
        # Honour the real signature: marker_path is the side-effect
        # the policy uses to decide "already bootstrapped" next time.
        marker = kwargs.get("marker_path")
        if marker is not None:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"completed_at": time.time()}))

    monkeypatch.setattr(
        "opencomputer.profile_bootstrap.orchestrator.run_bootstrap",
        fake_run_bootstrap,
    )

    thread = auto_trigger.kick_off_in_background()
    assert thread is not None
    thread.join(timeout=5.0)
    assert ran == [True], "expected run_bootstrap to be called inside the thread"

    # Marker should now exist → second call is a no-op.
    should, _reason = auto_trigger.should_auto_bootstrap()
    assert should is False


def test_kick_off_swallows_orchestrator_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If run_bootstrap raises, the chat loop must not crash. Error
    is logged at debug only."""
    from opencomputer.profile_bootstrap import auto_trigger

    _force_tty(monkeypatch)
    monkeypatch.delenv("OPENCOMPUTER_NO_AUTO_BOOTSTRAP", raising=False)

    def boom(**_):
        raise RuntimeError("synthetic failure inside bootstrap")

    monkeypatch.setattr(
        "opencomputer.profile_bootstrap.orchestrator.run_bootstrap", boom
    )

    thread = auto_trigger.kick_off_in_background()
    assert thread is not None
    thread.join(timeout=5.0)
    # Marker not written because run_bootstrap raised; next call
    # should still want to auto-bootstrap.
    should, _reason = auto_trigger.should_auto_bootstrap()
    assert should is True


def test_marker_path_lives_under_profile_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Marker path resolves to <profile_home>/profile_bootstrap/complete.json
    so it shares the same anchor cli_profile uses (single source of truth)."""
    from opencomputer.profile_bootstrap.auto_trigger import _marker_path

    expected = Path(os.environ["OPENCOMPUTER_HOME"]) / "profile_bootstrap" / "complete.json"
    assert _marker_path() == expected
