"""systemd-user service backend for the always-on gateway daemon.

Conforms to ``opencomputer.service.base.ServiceBackend`` Protocol via
module-level functions. The factory in ``service/factory.py`` returns
this module on Linux.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

from . import _common
from .base import InstallResult, StatusResult, UninstallResult

NAME: ClassVar[str] = "systemd"
_UNIT_FILENAME = "opencomputer.service"
_TEMPLATE = (Path(__file__).parent / "templates" / _UNIT_FILENAME).read_text()


def supported() -> bool:
    return sys.platform.startswith("linux")


def _user_unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user" / _UNIT_FILENAME


def _systemctl(*args: str) -> tuple[int, str, str]:
    if shutil.which("systemctl") is None:
        return (0, "", "(systemctl not found — skipping)")
    try:
        proc = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True, text=True, timeout=10,
        )
        return (proc.returncode, proc.stdout, proc.stderr)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return (1, "", str(exc))


def _resolve_executable() -> str:
    return _common.resolve_executable()


def _render_unit(executable: str, workdir: Path, profile: str, extra_args: str) -> str:
    return _TEMPLATE.format(
        executable=executable,
        workdir=str(workdir),
        profile=profile,
        extra_args=extra_args,
    )


def _is_lingering() -> bool:
    if shutil.which("loginctl") is None:
        return False
    try:
        proc = subprocess.run(
            ["loginctl", "show-user", os.environ.get("USER", ""), "--property=Linger"],
            capture_output=True, text=True, timeout=5,
        )
        return "Linger=yes" in proc.stdout
    except (subprocess.TimeoutExpired, OSError):
        return False


def _journalctl_tail(n: int) -> list[str]:
    if shutil.which("journalctl") is None:
        return []
    try:
        proc = subprocess.run(
            ["journalctl", "--user", "-u", _UNIT_FILENAME,
             "-n", str(n), "--no-pager"],
            capture_output=True, text=True, timeout=5,
        )
        return [ln for ln in proc.stdout.splitlines() if ln.strip()][-n:]
    except (subprocess.TimeoutExpired, OSError):
        return []


def install(*, profile: str, extra_args: str, restart: bool = True) -> InstallResult:
    executable = _resolve_executable()
    wd = _common.workdir(profile)
    body = _render_unit(executable, wd, profile, extra_args)
    path = _user_unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    _systemctl("daemon-reload")
    notes: list[str] = []
    enabled = False
    started = False
    if restart:
        rc_en, _, _ = _systemctl("enable", "--now", _UNIT_FILENAME)
        enabled = rc_en == 0
        if enabled:
            rc_act, out, _ = _systemctl("is-active", _UNIT_FILENAME)
            started = rc_act == 0 and out.strip() == "active"
    if not _is_lingering():
        notes.append(
            "On a headless Linux server, run "
            "`sudo loginctl enable-linger $USER` so the service keeps "
            "running across SSH disconnects.",
        )
    return InstallResult(
        backend=NAME, config_path=path,
        enabled=enabled, started=started, notes=notes,
    )


def uninstall() -> UninstallResult:
    path = _user_unit_path()
    if not path.exists():
        return UninstallResult(
            backend=NAME, file_removed=False, config_path=None, notes=[],
        )
    _systemctl("stop", _UNIT_FILENAME)
    _systemctl("disable", _UNIT_FILENAME)
    path.unlink()
    _systemctl("daemon-reload")
    return UninstallResult(
        backend=NAME, file_removed=True, config_path=path, notes=[],
    )


def status() -> StatusResult:
    path = _user_unit_path()
    file_present = path.exists()
    rc_en, out_en, _ = _systemctl("is-enabled", _UNIT_FILENAME)
    enabled = rc_en == 0 and out_en.strip() == "enabled"
    rc_ac, out_ac, _ = _systemctl("is-active", _UNIT_FILENAME)
    running = rc_ac == 0 and out_ac.strip() == "active"
    pid: int | None = None
    if running:
        rc_sh, out_sh, _ = _systemctl(
            "show", _UNIT_FILENAME,
            "-p", "MainPID,ActiveEnterTimestampMonotonic",
        )
        if rc_sh == 0:
            for line in out_sh.splitlines():
                if line.startswith("MainPID="):
                    try:
                        pid = int(line.split("=", 1)[1])
                        if pid == 0:
                            pid = None
                    except ValueError:
                        pid = None
    return StatusResult(
        backend=NAME,
        file_present=file_present,
        enabled=enabled,
        running=running,
        pid=pid,
        uptime_seconds=None,
        last_log_lines=_journalctl_tail(5),
    )


def start() -> bool:
    rc, _, _ = _systemctl("start", _UNIT_FILENAME)
    return rc == 0


def stop() -> bool:
    rc, _, _ = _systemctl("stop", _UNIT_FILENAME)
    return rc == 0


def follow_logs(*, lines: int = 100, follow: bool = False) -> Iterator[str]:
    if not follow:
        yield from _journalctl_tail(lines)
        return
    if shutil.which("journalctl") is None:
        return
    try:
        proc = subprocess.Popen(
            ["journalctl", "--user", "-u", _UNIT_FILENAME, "-f", "-n", str(lines)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except OSError:
        return
    try:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            yield line.rstrip()
    finally:
        proc.terminate()


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
