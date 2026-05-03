"""Windows Task Scheduler backend for the always-on gateway daemon.

User scope (no admin elevation). Triggered on login, restart-on-failure
configured in the task XML. Logs go to ``%USERPROFILE%\\.opencomputer\\<profile>\\logs``.
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

NAME: ClassVar[str] = "schtasks"
_TASK_NAME = "OpenComputerGateway"
_TEMPLATE = (Path(__file__).parent / "templates" / "opencomputer-task.xml").read_text()


def supported() -> bool:
    return sys.platform.startswith("win")


def _user_dir() -> Path:
    base = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(base) / ".opencomputer"


def _xml_path() -> Path:
    return _user_dir() / "opencomputer-task.xml"


def _resolve_executable() -> str:
    return _common.resolve_executable()


def _schtasks(*args: str) -> tuple[int, str, str]:
    if shutil.which("schtasks") is None:
        return (0, "", "(schtasks not found — skipping)")
    try:
        proc = subprocess.run(
            ["schtasks", *args],
            capture_output=True, text=True, timeout=10,
        )
        return (proc.returncode, proc.stdout, proc.stderr)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return (1, "", str(exc))


def _render_task(*, executable: str, workdir: Path, profile: str) -> str:
    return _TEMPLATE.format(
        executable=executable,
        workdir=str(workdir),
        profile=profile,
    )


def install(*, profile: str, extra_args: str, restart: bool = True) -> InstallResult:
    executable = _resolve_executable()
    wd = _common.workdir(profile)
    body = _render_task(executable=executable, workdir=wd, profile=profile)
    path = _xml_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-16")
    rc, _, err = _schtasks("/create", "/xml", str(path), "/tn", _TASK_NAME, "/f")
    enabled = rc == 0
    started = False
    if restart and enabled:
        rc_run, _, _ = _schtasks("/run", "/tn", _TASK_NAME)
        started = rc_run == 0
    notes: list[str] = []
    if not enabled:
        notes.append(f"schtasks /create returned {rc}: {err.strip()}")
    return InstallResult(
        backend=NAME, config_path=path,
        enabled=enabled, started=started, notes=notes,
    )


def uninstall() -> UninstallResult:
    _schtasks("/delete", "/tn", _TASK_NAME, "/f")
    path = _xml_path()
    if path.exists():
        path.unlink()
        return UninstallResult(
            backend=NAME, file_removed=True, config_path=path, notes=[],
        )
    return UninstallResult(
        backend=NAME, file_removed=False, config_path=None, notes=[],
    )


def status() -> StatusResult:
    path = _xml_path()
    file_present = path.exists()
    rc, out, _ = _schtasks("/query", "/tn", _TASK_NAME, "/v", "/fo", "list")
    enabled = rc == 0
    running = False
    if rc == 0:
        for line in out.splitlines():
            if line.startswith("Status:") and "Running" in line:
                running = True
                break
    out_log, _ = _common.log_paths("default")
    return StatusResult(
        backend=NAME,
        file_present=file_present,
        enabled=enabled,
        running=running,
        pid=None,
        uptime_seconds=None,
        last_log_lines=_common.tail_lines(out_log, 5),
    )


def start() -> bool:
    rc, _, _ = _schtasks("/run", "/tn", _TASK_NAME)
    return rc == 0


def stop() -> bool:
    rc, _, _ = _schtasks("/end", "/tn", _TASK_NAME)
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
