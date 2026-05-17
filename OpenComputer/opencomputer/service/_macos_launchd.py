"""macOS launchd backend for the always-on gateway daemon.

Uses the modern ``launchctl bootstrap gui/<uid>`` API (not the
deprecated ``launchctl load``). Distinct from ``service/launchd.py``
which is the daily profile-analyze cron — different concern, kept
side-by-side.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Final

from . import _common
from ._naming import _CANONICAL_LABEL, service_label
from .base import InstallResult, StatusResult, UninstallResult

NAME: Final[str] = "launchd"
_LEGACY_LABEL = "com.opencomputer.gateway"
_LEGACY_PLIST_FILENAME = f"{_LEGACY_LABEL}.plist"
_TEMPLATE = (Path(__file__).parent / "templates" / _LEGACY_PLIST_FILENAME).read_text()


def supported() -> bool:
    return sys.platform == "darwin"


def _label(profile: str = "default") -> str:
    """Return the launchd label for ``profile``.

    Default + canonical home preserves the historical
    ``com.opencomputer.gateway`` label so existing plists keep working.
    Multi-install (non-canonical home OR named profile) appends the
    sha256[:8] hash from ``service_label`` so two daemons can coexist.
    """
    label = service_label(profile)
    if label == _CANONICAL_LABEL:
        return _LEGACY_LABEL
    suffix = label.removeprefix(f"{_CANONICAL_LABEL}-")
    return f"{_LEGACY_LABEL}.{suffix}"


def _plist_filename(profile: str = "default") -> str:
    return f"{_label(profile)}.plist"


def _launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _plist_path(profile: str = "default") -> Path:
    return _launch_agents_dir() / _plist_filename(profile)


def _resolve_executable() -> str:
    return _common.resolve_executable()


def _uid() -> int:
    return os.getuid()


def _launchctl(*args: str) -> tuple[int, str, str]:
    if shutil.which("launchctl") is None:
        return (0, "", "(launchctl not found — skipping)")
    try:
        proc = subprocess.run(
            ["launchctl", *args],
            capture_output=True, text=True, timeout=10,
        )
        return (proc.returncode, proc.stdout, proc.stderr)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return (1, "", str(exc))


def _render_plist(
    *,
    executable: str,
    workdir: Path,
    profile: str,
    stdout_log: Path,
    stderr_log: Path,
) -> str:
    return _TEMPLATE.format(
        label=_label(profile),
        executable=executable,
        workdir=str(workdir),
        profile=profile,
        stdout_log=str(stdout_log),
        stderr_log=str(stderr_log),
    )


def install(*, profile: str, extra_args: str, restart: bool = True) -> InstallResult:
    executable = _resolve_executable()
    wd = _common.workdir(profile)
    out_log, err_log = _common.log_paths(profile)
    body = _render_plist(
        executable=executable, workdir=wd, profile=profile,
        stdout_log=out_log, stderr_log=err_log,
    )
    label = _label(profile)
    path = _plist_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _launchctl("bootout", f"gui/{_uid()}/{label}")
    path.write_text(body)
    started = False
    enabled = False
    if restart:
        rc, _, _ = _launchctl("bootstrap", f"gui/{_uid()}", str(path))
        enabled = rc == 0
        started = enabled
    return InstallResult(
        backend=NAME, config_path=path,
        enabled=enabled, started=started,
        notes=[],
    )


def uninstall(*, profile: str) -> UninstallResult:
    path = _plist_path(profile)
    if not path.exists():
        return UninstallResult(
            backend=NAME, file_removed=False, config_path=None, notes=[],
        )
    _launchctl("bootout", f"gui/{_uid()}/{_label(profile)}")
    path.unlink()
    return UninstallResult(
        backend=NAME, file_removed=True, config_path=path, notes=[],
    )


def status() -> StatusResult:
    path = _plist_path()
    file_present = path.exists()
    rc, out, _ = _launchctl("print", f"gui/{_uid()}/{_label()}")
    enabled = rc == 0
    running = False
    pid: int | None = None
    if rc == 0:
        # `launchctl print` prints `state =` multiple times: the first one
        # is the top-level service lifecycle state (e.g. "running" /
        # "not running" / "spawn scheduled"), and subsequent indented lines
        # describe attribute states ("active" / "inactive"). Reading every
        # line and overwriting `running` left it set by whichever
        # `state =` came LAST — on macOS Sequoia that's "active", which
        # doesn't contain the substring "running" — flipping the doctor
        # row to "enabled but not running" even when the daemon was up.
        # Take only the first `state =` line.
        state_seen = False
        for raw_line in out.splitlines():
            line = raw_line.strip()
            if not state_seen and line.startswith("state ="):
                running = "running" in line
                state_seen = True
            elif line.startswith("pid ="):
                try:
                    pid = int(line.split("=", 1)[1].strip())
                except ValueError:
                    pid = None
    out_log, _ = _common.log_paths("default")
    return StatusResult(
        backend=NAME,
        file_present=file_present,
        enabled=enabled,
        running=running,
        pid=pid,
        uptime_seconds=None,
        last_log_lines=_common.tail_lines(out_log, 5),
    )


def start() -> bool:
    """Kickstart the gateway service.

    Idempotent — if the plist exists but isn't bootstrapped, run a
    bootstrap first (covers the case where the user manually removed
    the service from launchd's domain via `launchctl bootout`).

    Multi-install aware: uses ``_label()`` (sha256-suffixed when
    OPENCOMPUTER_HOME is non-canonical) instead of static label.
    """
    label = _label()
    path = _plist_path()
    rc, _, _ = _launchctl("print", f"gui/{_uid()}/{label}")
    if rc != 0 and path.exists():
        rc_b, _, _ = _launchctl("bootstrap", f"gui/{_uid()}", str(path))
        if rc_b == 0:
            return True
    rc, _, _ = _launchctl("kickstart", "-k", f"gui/{_uid()}/{label}")
    return rc == 0


def stop() -> bool:
    """Cleanly stop the gateway service WITHOUT triggering a respawn.

    KeepAlive (even the dict form with SuccessfulExit=false) will
    re-bootstrap the service on a SIGTERM that exits cleanly. To
    actually keep it stopped, bootout the plist first — that removes
    it from launchd's domain so it can't respawn until the user runs
    `oc gateway start` (or the plist is bootstrapped again).

    Multi-install aware: uses ``_label()`` (sha256-suffixed when
    OPENCOMPUTER_HOME is non-canonical) instead of static label.

    Returns True iff the service was successfully stopped (or wasn't
    running to begin with).
    """
    label = _label()
    rc_print, _, _ = _launchctl("print", f"gui/{_uid()}/{label}")
    if rc_print != 0:
        # Already not loaded — nothing to do.
        return True
    # bootout removes from launchd's domain and sends SIGTERM in one
    # atomic operation. The KeepAlive policy can't trigger because
    # the service is no longer in the domain.
    rc, _, _ = _launchctl("bootout", f"gui/{_uid()}/{label}")
    if rc != 0:
        return False
    # Wait briefly for launchd to fully detach the daemon. Without this
    # delay, ``oc service restart`` races: bootout returns immediately
    # but the next ``bootstrap`` collides with the still-detaching
    # service and fails — surfacing as "restart failed: start step
    # failed". Polling launchctl print until rc != 0 confirms the
    # service is gone before stop() returns.
    for _ in range(20):  # ~2s max
        rc_check, _, _ = _launchctl("print", f"gui/{_uid()}/{label}")
        if rc_check != 0:
            return True
        time.sleep(0.1)
    # Bootout said success but service still listed; trust the bootout
    # return code rather than block indefinitely.
    return True


def follow_logs(*, lines: int = 100, follow: bool = False) -> Iterator[str]:
    out_log, _ = _common.log_paths("default")
    if not follow:
        yield from _common.tail_lines(out_log, lines)
        return
    pos = out_log.stat().st_size if out_log.exists() else 0
    yield from _common.tail_lines(out_log, lines)
    while True:
        if out_log.exists() and out_log.stat().st_size > pos:
            with out_log.open() as f:
                f.seek(pos)
                for line in f:
                    yield line.rstrip()
                pos = f.tell()
        time.sleep(1)


__all__ = [
    "NAME",
    "follow_logs",
    "install",
    "start",
    "status",
    "stop",
    "supported",
    "uninstall",
]
