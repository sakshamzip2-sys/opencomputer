"""Lifecycle unit tests — no real daemon spawn.

Each test isolates the lifecycle module to a temp profile home via
:func:`plugin_sdk.profile_context.set_profile`, then drives the
``status``/``start``/``stop`` surface with the Node binary mocked so
nothing real is launched.
"""

from __future__ import annotations

import importlib.util
import os
import signal
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

# File-path import to bypass the sys.modules collision against other
# plugins' lifecycle.py modules (none exists today, but the pattern
# matches what the plugin loader does at runtime).
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _import_lifecycle():
    path = _PLUGIN_ROOT / "lifecycle.py"
    spec = importlib.util.spec_from_file_location("_open_design_lifecycle_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_open_design_lifecycle_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def lifecycle(tmp_path: Path, monkeypatch):
    # Pin the profile home to a temp dir via OPENCOMPUTER_HOME — works
    # outside an asyncio Task and doesn't require entering the
    # set_profile context manager (which can only be used inside `with`).
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return _import_lifecycle()


def test_status_with_no_pid_file_reports_stopped(lifecycle) -> None:
    snap = lifecycle.status()
    assert snap.running is False
    assert snap.pid is None
    assert snap.port == lifecycle.DEFAULT_PORT
    assert snap.url == f"http://127.0.0.1:{lifecycle.DEFAULT_PORT}"


def test_status_cleans_stale_pid_file(lifecycle, tmp_path) -> None:
    pid_path = tmp_path / "locks" / "open-design.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    # PID 1 (init) exists on POSIX — would be incorrectly seen as alive.
    # Use a PID that almost certainly does not exist: 2^31 - 1.
    pid_path.write_text("2147483647")

    snap = lifecycle.status()
    assert snap.running is False
    assert not pid_path.exists()  # stale-cleanup happened


def test_resolve_home_via_env_override(lifecycle, tmp_path, monkeypatch) -> None:
    # Synthesise a fake open-design tree.
    fake_home = tmp_path / "open-design"
    (fake_home / "apps" / "daemon").mkdir(parents=True)
    (fake_home / "apps" / "daemon" / "package.json").write_text("{}")
    monkeypatch.setenv("OPEN_DESIGN_HOME", str(fake_home))

    found = lifecycle.resolve_open_design_home()
    assert found == fake_home


def test_resolve_home_returns_none_when_missing(lifecycle, monkeypatch) -> None:
    monkeypatch.setenv("OPEN_DESIGN_HOME", "/nonexistent/path/that/should/not/exist")
    # Other candidate paths probably don't exist in CI either — but on
    # saksham's laptop ~/Vscode/claude/open-design *does* exist. Skip when so.
    found = lifecycle.resolve_open_design_home()
    if found is not None:
        pytest.skip("default candidate path exists on this machine")
    assert found is None


def test_start_without_open_design_raises(lifecycle, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPEN_DESIGN_HOME", str(tmp_path / "does-not-exist"))
    with pytest.raises(lifecycle.OpenDesignNotInstalledError):
        lifecycle.start()


def test_start_with_unbuilt_source_raises(lifecycle, tmp_path, monkeypatch) -> None:
    src = tmp_path / "od-src"
    (src / "apps" / "daemon").mkdir(parents=True)
    (src / "apps" / "daemon" / "package.json").write_text("{}")
    # No built dist/cli.js → should raise with build hint
    monkeypatch.setenv("OPEN_DESIGN_HOME", str(src))
    with pytest.raises(lifecycle.OpenDesignNotInstalledError, match="not built"):
        lifecycle.start()


def test_stop_when_not_running_is_noop(lifecycle) -> None:
    snap = lifecycle.stop()
    assert snap.running is False


def test_status_json_roundtrip(lifecycle) -> None:
    import json
    payload = json.loads(lifecycle.status_json())
    assert "running" in payload
    assert "port" in payload
    assert "url" in payload


def test_is_alive_negative_pid(lifecycle) -> None:
    # Internal helper: reject obviously-invalid PIDs.
    assert lifecycle._is_alive(0) is False
    assert lifecycle._is_alive(-1) is False


def test_port_override_via_env(lifecycle, monkeypatch) -> None:
    monkeypatch.setenv("OD_PORT", "9999")
    # _resolve_port is module-private; we test through status().
    snap = lifecycle.status()
    assert snap.port == 9999


def test_port_override_garbage_falls_back(lifecycle, monkeypatch) -> None:
    monkeypatch.setenv("OD_PORT", "not-a-number")
    snap = lifecycle.status()
    assert snap.port == lifecycle.DEFAULT_PORT


def test_port_below_min_falls_back(lifecycle, monkeypatch) -> None:
    """Privileged port (< 1024) → safe default, not a permission error."""
    monkeypatch.setenv("OD_PORT", "80")
    snap = lifecycle.status()
    assert snap.port == lifecycle.DEFAULT_PORT


def test_port_above_max_falls_back(lifecycle, monkeypatch) -> None:
    """Invalid port (> 65535) → safe default, not a RangeError."""
    monkeypatch.setenv("OD_PORT", "70000")
    snap = lifecycle.status()
    assert snap.port == lifecycle.DEFAULT_PORT


def test_port_at_min_boundary_accepted(lifecycle, monkeypatch) -> None:
    monkeypatch.setenv("OD_PORT", "1024")
    snap = lifecycle.status()
    assert snap.port == 1024


def test_port_at_max_boundary_accepted(lifecycle, monkeypatch) -> None:
    monkeypatch.setenv("OD_PORT", "65535")
    snap = lifecycle.status()
    assert snap.port == 65535


def test_validate_port_helper_clamps(lifecycle) -> None:
    """Internal helper — explicit positional verification."""
    assert lifecycle._validate_port(7456, source="test") == 7456
    assert lifecycle._validate_port(80, source="test") == lifecycle.DEFAULT_PORT
    assert lifecycle._validate_port(70_000, source="test") == lifecycle.DEFAULT_PORT
    assert lifecycle._validate_port(0, source="test") == lifecycle.DEFAULT_PORT
    assert lifecycle._validate_port(-1, source="test") == lifecycle.DEFAULT_PORT
