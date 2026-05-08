"""Regression tests for the 2026-05-08 launchd respawn-loop incident.

The bug: ``KeepAlive=<true/>`` in both plist templates meant launchd
respawned the gateway daemon even after intentional ``kill -9`` /
``oc service stop``, making the daemon impossible to actually stop.
A 33-hour SQLite-error loop couldn't be killed because every SIGKILL
triggered an immediate respawn.

The fix: change ``KeepAlive`` to a dict-style policy
``{SuccessfulExit=false, Crashed=true}`` — only respawn on crash, not
on intentional exit. The macOS service backend's ``stop()`` was also
changed to ``launchctl bootout`` (atomic remove-from-domain + SIGTERM)
since even the dict form of KeepAlive can re-bootstrap on a SIGTERM
clean exit if the service is still in launchd's domain.

These tests guard both layers from drift.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from opencomputer.service import _macos_launchd

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON_TEMPLATE = (
    REPO_ROOT
    / "opencomputer" / "service" / "templates" / "com.opencomputer.gateway.plist"
)


def test_python_template_keepalive_uses_dict_form() -> None:
    """The Python-managed plist template must NOT use ``KeepAlive=<true/>``.

    Mirrors the test in ``tests/test_launchd_plist.py`` for the older
    shell-managed template — both must agree on the dict form.
    """
    raw = PYTHON_TEMPLATE.read_text()
    keep_idx = raw.index("<key>KeepAlive</key>")
    after = raw[keep_idx + len("<key>KeepAlive</key>"):].lstrip()
    assert not after.startswith("<true/>"), (
        "KeepAlive uses raw <true/> — convert to <dict>...</dict>. "
        "See 2026-05-08 respawn-loop incident in the plist comments."
    )
    assert "<key>SuccessfulExit</key>" in raw
    assert "<key>Crashed</key>" in raw


def test_python_template_renders_with_correct_keepalive() -> None:
    """Round-trip the template through ``_render_plist`` to ensure the
    dict form survives substitution and ends up in the user's plist."""
    body = _macos_launchd._render_plist(
        executable="/usr/local/bin/oc",
        workdir=Path("/Users/test/.opencomputer/default"),
        profile="default",
        stdout_log=Path("/tmp/out.log"),
        stderr_log=Path("/tmp/err.log"),
    )
    # The render must still contain the dict-form KeepAlive
    keep_idx = body.index("<key>KeepAlive</key>")
    after = body[keep_idx + len("<key>KeepAlive</key>"):].lstrip()
    assert not after.startswith("<true/>"), (
        "Rendered plist regressed to KeepAlive=<true/>"
    )


def test_stop_uses_bootout_not_kill() -> None:
    """``stop()`` must call ``launchctl bootout`` (atomic remove-from-
    domain + SIGTERM) so the KeepAlive policy can't trigger a respawn.

    Calling ``launchctl kill SIGTERM`` alone leaves the service in the
    domain, and the dict-form KeepAlive will still re-bootstrap on
    Crashed=true if SIGTERM is treated as a crash.
    """
    captured: list[tuple[str, ...]] = []

    def fake_launchctl(*args: str) -> tuple[int, str, str]:
        captured.append(args)
        # First call is "print" (state probe); return rc=0 (loaded).
        if args[0] == "print":
            return (0, "state = running", "")
        # Subsequent: bootout / kill etc.
        return (0, "", "")

    with patch.object(_macos_launchd, "_launchctl", fake_launchctl):
        ok = _macos_launchd.stop()
    assert ok is True
    # Should have called: print (probe), bootout (atomic stop)
    cmds = [args[0] for args in captured]
    assert "bootout" in cmds, f"stop() must use bootout; called: {cmds}"
    # Should NOT use raw kill SIGTERM — that lets KeepAlive respawn.
    for args in captured:
        if args and args[0] == "kill":
            raise AssertionError(
                f"stop() regressed to ``launchctl kill``: {args}"
            )


def test_stop_returns_true_when_not_loaded() -> None:
    """If the service isn't in launchd's domain, ``stop()`` is a no-op
    that succeeds (idempotent contract)."""
    def fake_launchctl(*args: str) -> tuple[int, str, str]:
        # "print" returns non-zero → service not loaded
        return (1, "", "Could not find service")

    with patch.object(_macos_launchd, "_launchctl", fake_launchctl):
        ok = _macos_launchd.stop()
    assert ok is True


def test_start_recovers_from_booted_out_state(tmp_path: Path) -> None:
    """If the plist exists but isn't bootstrapped (e.g. user ran
    ``oc service stop``), ``start()`` should bootstrap+kickstart, not
    just kickstart-which-fails."""
    fake_plist = tmp_path / "com.opencomputer.gateway.plist"
    fake_plist.write_text("<plist><dict/></plist>")

    captured: list[tuple[str, ...]] = []

    def fake_launchctl(*args: str) -> tuple[int, str, str]:
        captured.append(args)
        if args[0] == "print":
            return (1, "", "Could not find service")  # not loaded
        if args[0] == "bootstrap":
            return (0, "", "")  # bootstrap succeeds
        return (0, "", "")

    with patch.object(_macos_launchd, "_plist_path", lambda: fake_plist), \
         patch.object(_macos_launchd, "_launchctl", fake_launchctl):
        ok = _macos_launchd.start()
    assert ok is True
    cmds = [args[0] for args in captured]
    assert "bootstrap" in cmds, (
        f"start() must bootstrap when service is not loaded; called: {cmds}"
    )
