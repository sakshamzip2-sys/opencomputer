"""macOS launchd gateway backend (NOT the daily profile-analyze cron)."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_render_plist_substitutes_fields(tmp_path: Path) -> None:
    from opencomputer.service import _macos_launchd

    body = _macos_launchd._render_plist(
        executable="/opt/homebrew/bin/oc",
        workdir=tmp_path,
        profile="default",
        stdout_log=tmp_path / "stdout.log",
        stderr_log=tmp_path / "stderr.log",
    )
    assert "<string>/opt/homebrew/bin/oc</string>" in body
    assert "<string>--profile</string>" in body
    assert "<string>default</string>" in body
    assert "<key>KeepAlive</key>" in body
    assert "<true/>" in body
    # Plist is well-formed XML
    import xml.etree.ElementTree as ET
    ET.fromstring(body)
    # argv ends at "gateway" (no "run")
    assert "<string>run</string>" not in body


def test_install_writes_plist_and_calls_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.service import _macos_launchd

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(_macos_launchd, "_resolve_executable", lambda: "/usr/local/bin/oc")
    monkeypatch.setattr(_macos_launchd, "_uid", lambda: 501)

    calls: list[tuple[str, ...]] = []

    def fake_launchctl(*a):
        calls.append(a)
        return (0, "", "")

    monkeypatch.setattr(_macos_launchd, "_launchctl", fake_launchctl)

    result = _macos_launchd.install(profile="default", extra_args="")
    # Multi-install hashing: the plist filename / launchd label is
    # 'com.opencomputer.gateway' on canonical home but
    # 'com.opencomputer.gateway.<hash>' on a non-canonical home (CI runners).
    # Mirror what production uses (_label) rather than asserting a static name.
    label = _macos_launchd._label("default")
    expected = fake_home / "Library" / "LaunchAgents" / f"{label}.plist"
    assert result.config_path == expected
    assert expected.exists()
    body = expected.read_text()
    assert label in body
    bootstrap_calls = [c for c in calls if c[:1] == ("bootstrap",)]
    assert bootstrap_calls
    assert bootstrap_calls[0][1] == "gui/501"


def test_uninstall_removes_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.service import _macos_launchd

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(_macos_launchd, "_resolve_executable", lambda: "/usr/local/bin/oc")
    monkeypatch.setattr(_macos_launchd, "_uid", lambda: 501)
    monkeypatch.setattr(_macos_launchd, "_launchctl", lambda *a: (0, "", ""))

    install_result = _macos_launchd.install(profile="default", extra_args="")
    assert install_result.config_path.exists()

    uninstall_result = _macos_launchd.uninstall()
    assert uninstall_result.file_removed is True
    assert not install_result.config_path.exists()


def test_supported_returns_true_only_on_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service import _macos_launchd

    monkeypatch.setattr("sys.platform", "darwin")
    assert _macos_launchd.supported() is True
    monkeypatch.setattr("sys.platform", "linux")
    assert _macos_launchd.supported() is False


def test_status_parses_launchctl_print(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.service import _macos_launchd

    monkeypatch.setattr(_macos_launchd, "_uid", lambda: 501)

    fake_plist = tmp_path / "com.opencomputer.gateway.plist"
    fake_plist.write_text("(stub)")
    monkeypatch.setattr(_macos_launchd, "_plist_path", lambda: fake_plist)

    sample_print = """\
gui/501/com.opencomputer.gateway = {
\tactive count = 1
\tpath = /Users/me/Library/LaunchAgents/com.opencomputer.gateway.plist
\ttype = LaunchAgent
\tstate = running
\tpid = 91234
}"""

    def fake_launchctl(*args):
        if args[0] == "print":
            return (0, sample_print, "")
        return (0, "", "")

    monkeypatch.setattr(_macos_launchd, "_launchctl", fake_launchctl)
    monkeypatch.setattr(
        "opencomputer.service._common.tail_lines",
        lambda p, n: ["log a", "log b"],
    )

    s = _macos_launchd.status()
    assert s.backend == "launchd"
    assert s.file_present is True
    assert s.running is True
    assert s.pid == 91234


def test_status_first_state_line_wins_when_multiple_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: macOS Sequoia's `launchctl print` emits 3 `state =`
    lines. The first is the lifecycle state (running / not running);
    the next two are attribute states (active / inactive). Previously
    the loop overwrote `running` for every match, so the LAST line —
    which says ``state = active``, NOT ``state = running`` — won and
    the function returned running=False even when the daemon was up."""
    from opencomputer.service import _macos_launchd

    monkeypatch.setattr(_macos_launchd, "_uid", lambda: 501)
    fake_plist = tmp_path / "com.opencomputer.gateway.plist"
    fake_plist.write_text("(stub)")
    monkeypatch.setattr(_macos_launchd, "_plist_path", lambda: fake_plist)

    # Real macOS Sequoia output shape — three `state =` lines.
    sample_print = """\
gui/501/com.opencomputer.gateway = {
\tactive count = 1
\tstate = running
\tpid = 96119
\tnested-thing = {
\t\tstate = active
\t}
\tanother-nested = {
\t\tstate = active
\t}
}"""

    monkeypatch.setattr(
        _macos_launchd,
        "_launchctl",
        lambda *args: (0, sample_print, "") if args[0] == "print" else (0, "", ""),
    )
    monkeypatch.setattr(
        "opencomputer.service._common.tail_lines",
        lambda p, n: [],
    )

    s = _macos_launchd.status()
    assert s.running is True, (
        "first `state =` line was 'running' — top-level lifecycle state — "
        "the function must NOT be fooled by subsequent attribute-state lines"
    )
    assert s.pid == 96119


def test_start_kickstart(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service import _macos_launchd

    monkeypatch.setattr(_macos_launchd, "_uid", lambda: 501)
    calls: list = []
    monkeypatch.setattr(
        _macos_launchd, "_launchctl",
        lambda *a: (calls.append(a) or (0, "", "")),
    )
    assert _macos_launchd.start() is True
    assert any(c[0] == "kickstart" for c in calls)


def test_stop_polls_until_service_detached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: stop() must wait for launchd to fully detach before
    returning. Without the post-bootout poll, ``oc service restart``
    races — bootout returns immediately but the next bootstrap collides
    with the still-detaching service and the restart "start step" fails."""
    from opencomputer.service import _macos_launchd

    monkeypatch.setattr(_macos_launchd, "_uid", lambda: 501)
    monkeypatch.setattr(_macos_launchd.time, "sleep", lambda _s: None)
    print_call_count = 0

    def fake_launchctl(*args: str) -> tuple[int, str, str]:
        nonlocal print_call_count
        if args[0] == "print":
            print_call_count += 1
            # First print call: service exists (rc=0). After 3 polls, gone.
            if print_call_count <= 3:
                return (0, "state = running", "")
            return (1, "", "Could not find service")
        # bootout always succeeds in this fixture.
        return (0, "", "")

    monkeypatch.setattr(_macos_launchd, "_launchctl", fake_launchctl)
    assert _macos_launchd.stop() is True
    # Print called: 1 initial state-check + N polls until service is gone.
    assert print_call_count >= 2, f"stop() should poll print after bootout; got {print_call_count}"


def test_stop_uses_bootout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Updated 2026-05-08 (PR #489): stop() now uses ``launchctl bootout``
    instead of ``launchctl kill SIGTERM``. With KeepAlive=dict, a clean
    SIGTERM exit can still cause launchd to re-bootstrap; bootout
    atomically removes the service from the domain so KeepAlive can't
    trigger. See _macos_launchd.stop() docstring for the full rationale."""
    from opencomputer.service import _macos_launchd

    monkeypatch.setattr(_macos_launchd, "_uid", lambda: 501)
    monkeypatch.setattr(_macos_launchd.time, "sleep", lambda _s: None)
    calls: list = []

    def fake_launchctl(*args: str) -> tuple[int, str, str]:
        calls.append(args)
        # First call is "print" (state probe); return rc=0 (loaded).
        if args[0] == "print":
            return (0, "state = running", "")
        return (0, "", "")

    monkeypatch.setattr(_macos_launchd, "_launchctl", fake_launchctl)
    assert _macos_launchd.stop() is True
    cmds = [c[0] for c in calls]
    assert "bootout" in cmds, f"stop() must use bootout (not kill); called: {cmds}"
    # Regression guard: stop() MUST NOT use raw `kill SIGTERM` because
    # KeepAlive=dict can re-bootstrap on a clean exit if the service
    # is still in launchd's domain.
    for c in calls:
        if c and c[0] == "kill":
            raise AssertionError(f"stop() regressed to launchctl kill: {c}")


def test_follow_logs_tails_stdout_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.service import _macos_launchd

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    log_dir = tmp_path / ".opencomputer" / "default" / "logs"
    log_dir.mkdir(parents=True)
    out_log = log_dir / "gateway.stdout.log"
    out_log.write_text("\n".join(f"line {i}" for i in range(10)) + "\n")

    out = list(_macos_launchd.follow_logs(lines=3, follow=False))
    assert out[-3:] == ["line 7", "line 8", "line 9"]
