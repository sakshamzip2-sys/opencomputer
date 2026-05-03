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
from typing import ClassVar

from . import _common
from .base import InstallResult, StatusResult, UninstallResult

NAME: ClassVar[str] = "launchd"
_LABEL = "com.opencomputer.gateway"
_PLIST_FILENAME = f"{_LABEL}.plist"
_TEMPLATE = (Path(__file__).parent / "templates" / _PLIST_FILENAME).read_text()


def supported() -> bool:
    return sys.platform == "darwin"


def _launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _plist_path() -> Path:
    return _launch_agents_dir() / _PLIST_FILENAME


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
        label=_LABEL,
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
    path = _plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _launchctl("bootout", f"gui/{_uid()}/{_LABEL}")
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


def uninstall() -> UninstallResult:
    path = _plist_path()
    if not path.exists():
        return UninstallResult(
            backend=NAME, file_removed=False, config_path=None, notes=[],
        )
    _launchctl("bootout", f"gui/{_uid()}/{_LABEL}")
    path.unlink()
    return UninstallResult(
        backend=NAME, file_removed=True, config_path=path, notes=[],
    )


def status() -> StatusResult:
    path = _plist_path()
    file_present = path.exists()
    rc, out, _ = _launchctl("print", f"gui/{_uid()}/{_LABEL}")
    enabled = rc == 0
    running = False
    pid: int | None = None
    if rc == 0:
        for raw_line in out.splitlines():
            line = raw_line.strip()
            if line.startswith("state ="):
                running = "running" in line
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
    rc, _, _ = _launchctl("kickstart", "-k", f"gui/{_uid()}/{_LABEL}")
    return rc == 0


def stop() -> bool:
    rc, _, _ = _launchctl("kill", "SIGTERM", f"gui/{_uid()}/{_LABEL}")
    return rc == 0


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
