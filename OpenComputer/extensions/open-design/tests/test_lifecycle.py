"""Lifecycle unit tests — no real daemon spawn.

Each test isolates the lifecycle module to a temp profile home via
:func:`plugin_sdk.profile_context.set_profile`, then drives the
``status``/``start``/``stop`` surface with the Node binary mocked so
nothing real is launched.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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


def test_port_in_use_helper_detects_listener(lifecycle) -> None:
    """Bind a socket, verify _port_in_use sees it; close, verify clear.

    Uses port 0 so the OS picks a free ephemeral port — guaranteed not
    to conflict with anything else on the test runner.
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        assert lifecycle._port_in_use(port) is True
    finally:
        sock.close()
    # After close, should be free again (allow a small grace for TIME_WAIT;
    # in practice immediate re-bind succeeds because the test socket was
    # never connected).
    assert lifecycle._port_in_use(port) is False


def test_start_raises_port_in_use_when_squatter_present(
    lifecycle, tmp_path, monkeypatch,
) -> None:
    """Spawn a stub Node-free 'daemon' source tree, bind the daemon's
    port from this test, and verify start() raises PortInUseError.

    Importantly, this triggers the new guard BEFORE Popen runs — so
    no zombie subprocess is left behind even though the daemon binary
    points to a path that wouldn't execute. We bind on the resolved
    port AFTER computing it (via OD_PORT) to avoid racing the OS.
    """
    import socket

    # Synthetic open-design tree with a built daemon entry (file just
    # needs to exist; we won't reach Popen).
    fake = tmp_path / "od"
    daemon_dir = fake / "apps" / "daemon"
    (daemon_dir / "dist").mkdir(parents=True)
    (daemon_dir / "dist" / "cli.js").write_text("// stub")
    monkeypatch.setenv("OPEN_DESIGN_HOME", str(fake))

    # Pick a free port via OS, then hold it open + tell start() to use it.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        with pytest.raises(lifecycle.PortInUseError, match=str(port)):
            lifecycle.start(port=port)
    finally:
        sock.close()


def test_probe_spa_index_rejects_cannot_get(lifecycle) -> None:
    """Helper unit test — synthesise the daemon's 'Cannot GET /' body
    via a small HTTPServer and verify _probe_spa_index returns False."""
    import http.server
    import threading

    class CannotGetHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — stdlib API
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body><pre>Cannot GET /</pre></body></html>")

        def log_message(self, *_args):  # silence stderr
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), CannotGetHandler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{port}"
        assert lifecycle._probe_spa_index(url) is False
    finally:
        server.shutdown()


def test_probe_spa_index_accepts_real_spa(lifecycle) -> None:
    """Helper unit test — synthesise a 200 HTML page that looks like a
    Next.js SPA and verify _probe_spa_index returns True."""
    import http.server
    import threading

    class SpaHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<!DOCTYPE html><html lang='en'><head><title>SPA</title></head></html>")

        def log_message(self, *_args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), SpaHandler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{port}"
        assert lifecycle._probe_spa_index(url) is True
    finally:
        server.shutdown()


def test_daemon_status_includes_web_served(lifecycle) -> None:
    """DaemonStatus dataclass exposes web_served; default is False."""
    snap = lifecycle.DaemonStatus(
        running=False,
        pid=None,
        port=7456,
        url="http://127.0.0.1:7456",
        home=None,
        log_path=lifecycle._log_file(),
    )
    assert snap.web_served is False
    assert snap.to_dict()["web_served"] is False


def test_log_rotation_drops_oldest_and_shifts(lifecycle, tmp_path) -> None:
    """Log over threshold → active becomes .log.1, .log.1 → .log.2,
    .log.2 → .log.3, prior .log.3 deleted. Underneath the threshold,
    no rotation."""
    log = lifecycle._log_file()
    log.parent.mkdir(parents=True, exist_ok=True)

    # Set up a pre-rotation chain at the keep limit.
    log.write_bytes(b"X" * (lifecycle.LOG_ROTATE_THRESHOLD_BYTES + 1))
    log.with_suffix(".log.1").write_text("rotated-1")
    log.with_suffix(".log.2").write_text("rotated-2")
    log.with_suffix(".log.3").write_text("rotated-3")  # should be deleted

    lifecycle._rotate_log_if_needed()

    # Active log moved to slot 1.
    assert not log.exists() or log.stat().st_size == 0
    assert log.with_suffix(".log.1").exists()
    # The previous .log.1 ("rotated-1") now lives at .log.2.
    assert log.with_suffix(".log.2").read_text() == "rotated-1"
    # The previous .log.2 ("rotated-2") now lives at .log.3.
    assert log.with_suffix(".log.3").read_text() == "rotated-2"
    # The pre-existing .log.3 was dropped (size threshold for keep=3).
    # The text "rotated-3" must no longer appear anywhere.
    for slot in range(1, lifecycle.LOG_ROTATE_KEEP + 1):
        path = log.with_suffix(f".log.{slot}")
        if path.exists():
            assert "rotated-3" not in path.read_text()


def test_log_rotation_skips_when_under_threshold(lifecycle) -> None:
    log = lifecycle._log_file()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("small")  # well under threshold

    lifecycle._rotate_log_if_needed()

    assert log.read_text() == "small"
    assert not log.with_suffix(".log.1").exists()


def test_log_rotation_no_op_when_log_missing(lifecycle) -> None:
    """Missing log file → silently return, no rotation files created."""
    log = lifecycle._log_file()
    if log.exists():
        log.unlink()
    lifecycle._rotate_log_if_needed()
    assert not log.with_suffix(".log.1").exists()


# ── Doctor contributions ─────────────────────────────────────────────


@pytest.fixture
def doctor_module():
    """Load doctor.py via spec_from_file_location to avoid sys.modules
    collisions with the same name in other plugins.

    doctor.py does ``from lifecycle import …`` which needs the plugin
    root on sys.path[0] — the loader puts it there in production, but
    tests have to do it themselves.
    """
    import importlib.util

    plugin_root_str = str(_PLUGIN_ROOT)
    if plugin_root_str not in sys.path:
        sys.path.insert(0, plugin_root_str)

    # Pre-load lifecycle under its plain name so `from lifecycle import …`
    # inside doctor.py resolves to the same module the test fixture uses.
    if "lifecycle" not in sys.modules:
        spec_lc = importlib.util.spec_from_file_location(
            "lifecycle", _PLUGIN_ROOT / "lifecycle.py",
        )
        assert spec_lc is not None and spec_lc.loader is not None
        lc_mod = importlib.util.module_from_spec(spec_lc)
        sys.modules["lifecycle"] = lc_mod
        spec_lc.loader.exec_module(lc_mod)

    spec = importlib.util.spec_from_file_location(
        "_open_design_doctor_test", _PLUGIN_ROOT / "doctor.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_open_design_doctor_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_doctor_contributions_have_typed_status(doctor_module) -> None:
    """Every contribution must return a RepairResult with one of the
    four typed status literals — pass/warn/fail/skip. Regression test
    for the original bug where I called RepairResult(ok=..., message=...)
    against an API that requires id+status+detail."""
    import asyncio

    contributions = doctor_module.build_contributions()
    assert len(contributions) == 5, "expected 5 doctor rows"

    valid_status = {"pass", "warn", "fail", "skip"}
    for c in contributions:
        result = asyncio.run(c.run(False))
        assert result.id == c.id, f"id mismatch: {result.id} vs {c.id}"
        assert result.status in valid_status, (
            f"{c.id} returned invalid status {result.status!r}; "
            f"must be one of {valid_status}"
        )
        assert isinstance(result.detail, str)
        assert result.repaired is False  # fix=False → no mutation


def test_doctor_home_check_passes_when_resolved(doctor_module, monkeypatch, tmp_path) -> None:
    import asyncio
    fake = tmp_path / "od"
    (fake / "apps" / "daemon" / "dist").mkdir(parents=True)
    (fake / "apps" / "daemon" / "dist" / "cli.js").write_text("// stub")
    monkeypatch.setenv("OPEN_DESIGN_HOME", str(fake))

    home_check = next(
        c for c in doctor_module.build_contributions() if c.id == "open-design.home"
    )
    result = asyncio.run(home_check.run(False))
    assert result.status == "pass"
    assert str(fake) in result.detail


def test_doctor_home_check_skips_when_unresolved(doctor_module, monkeypatch) -> None:
    """When open-design is not installed, the doctor row should `skip`
    (not `fail`) — the plugin is auto-enabled but open-design itself is
    optional. Aggregate failure count in `oc doctor` stays clean."""
    import asyncio
    monkeypatch.setenv("OPEN_DESIGN_HOME", "/nonexistent/path-that-doesnt-exist")

    home_check = next(
        c for c in doctor_module.build_contributions() if c.id == "open-design.home"
    )
    result = asyncio.run(home_check.run(False))
    # The default candidate paths may still resolve on this machine.
    if result.status == "pass":
        pytest.skip("default candidate path exists on this machine")
    assert result.status == "skip"
    assert "optional" in result.detail.lower()
