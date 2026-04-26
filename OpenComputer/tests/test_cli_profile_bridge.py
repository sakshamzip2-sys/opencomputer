"""Layered Awareness MVP — bridge CLI subcommand tests."""
import stat as _stat
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from opencomputer.cli_profile import profile_app

runner = CliRunner()


def test_bridge_token_creates_and_prints(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    result = runner.invoke(profile_app, ["bridge", "token"])
    assert result.exit_code == 0
    # Token is URL-safe base64-ish, length > 32
    out = result.stdout.strip().splitlines()[-1]
    assert len(out) >= 32


def test_bridge_token_idempotent(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    first = runner.invoke(profile_app, ["bridge", "token"]).stdout.strip().splitlines()[-1]
    second = runner.invoke(profile_app, ["bridge", "token"]).stdout.strip().splitlines()[-1]
    assert first == second  # second call returns the existing token


def test_bridge_token_rotate(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    first = runner.invoke(profile_app, ["bridge", "token"]).stdout.strip().splitlines()[-1]
    second = runner.invoke(
        profile_app, ["bridge", "token", "--rotate"]
    ).stdout.strip().splitlines()[-1]
    assert first != second


def test_bridge_status_reports_reachable(tmp_path: Path, monkeypatch):
    """Status REACHABLE when localhost connect succeeds."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner.invoke(profile_app, ["bridge", "token"])  # seed token

    fake_sock = MagicMock()
    fake_sock.connect.return_value = None  # connect succeeds (returns None)

    with patch("socket.socket", return_value=fake_sock):
        result = runner.invoke(profile_app, ["bridge", "status"])
    assert result.exit_code == 0
    assert "REACHABLE" in result.stdout
    assert "NOT REACHABLE" not in result.stdout
    assert "Bind port: 18791" in result.stdout


def test_bridge_status_reports_unreachable(tmp_path: Path, monkeypatch):
    """Status NOT REACHABLE when localhost connect raises OSError."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner.invoke(profile_app, ["bridge", "token"])  # seed token

    fake_sock = MagicMock()
    fake_sock.connect.side_effect = OSError("connection refused")

    with patch("socket.socket", return_value=fake_sock):
        result = runner.invoke(profile_app, ["bridge", "status"])
    assert result.exit_code == 0
    assert "NOT REACHABLE" in result.stdout


def test_bridge_start_no_token_errors(tmp_path: Path, monkeypatch):
    """``bridge start`` must abort with a clear message when no token is set.

    We can't actually exercise the listener startup from a unit test —
    that's a foreground ``asyncio.Event().wait()`` blocking call — but
    we can confirm the no-token guard fires before any aiohttp imports
    happen. Exit code 1 + a recognisable message is the contract; if
    this fails the production path either silently no-ops or raises an
    opaque error.
    """
    import json as _json

    from opencomputer.profile_bootstrap.bridge_state import state_path

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    # token() generates a fresh state file with a populated token; we
    # then overwrite it with an empty token to exercise the guard.
    runner.invoke(profile_app, ["bridge", "token"])
    p = state_path()
    p.write_text(_json.dumps({"token": "", "port": 18791}))

    result = runner.invoke(profile_app, ["bridge", "start"])
    assert result.exit_code == 1
    combined = (result.stdout or "") + (result.stderr or "")
    assert "No token configured" in combined


def test_bridge_state_file_is_owner_readable_only(tmp_path: Path, monkeypatch):
    """bridge.json must be chmod 0o600 — token shouldn't leak via world-readable file."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.profile_bootstrap.bridge_state import (
        load_or_create,
        state_path,
    )

    load_or_create()  # creates the state file
    p = state_path()
    mode = _stat.S_IMODE(p.stat().st_mode)
    assert mode == 0o600


def test_bridge_state_rotate_restores_permissions(tmp_path: Path, monkeypatch):
    """Rotating the token must re-chmod the file."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.profile_bootstrap.bridge_state import (
        load_or_create,
        state_path,
    )

    load_or_create()
    # Manually weaken perms to verify rotation re-tightens them
    import os
    p = state_path()
    os.chmod(p, 0o644)
    assert _stat.S_IMODE(p.stat().st_mode) == 0o644

    load_or_create(rotate=True)
    assert _stat.S_IMODE(p.stat().st_mode) == 0o600


def test_bridge_stop_no_listener(tmp_path: Path, monkeypatch):
    """``bridge stop`` is a no-op when nothing is listening on the port.

    Returns exit 0 with an informative message rather than blowing up,
    so it's safe to invoke unconditionally from cleanup scripts.
    """
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner.invoke(profile_app, ["bridge", "token"])  # seed token
    # Use port 1 (privileged + almost certainly unused by user processes)
    # so the lsof probe reliably returns nothing. We don't override the
    # state file's port because the lsof check uses whatever's saved.
    result = runner.invoke(profile_app, ["bridge", "stop"])
    # Either: lsof not available → exit 1 with message, OR lsof found
    # nothing → exit 0 with "nothing to stop". Both are acceptable.
    combined = (result.stdout or "") + (result.stderr or "")
    if result.exit_code == 0:
        assert "nothing to stop" in combined
    else:
        assert result.exit_code == 1
        assert "lsof not found" in combined
