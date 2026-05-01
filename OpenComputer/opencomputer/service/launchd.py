"""launchd-user plist install/uninstall for the daily profile-analyze cron.

macOS-only sibling of ``service/__init__.py`` (which handles systemd on
Linux). Installs into ``~/Library/LaunchAgents/`` and uses
``launchctl bootstrap`` / ``bootout`` to load/unload the service.

Runs daily at 9am local via ``StartCalendarInterval``. Logs to
``~/.opencomputer/profile-analyze.log``. ``RunAtLoad=false`` so install
doesn't trigger an immediate analyze run — the user explicitly runs
``oc profile analyze run`` for that.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_TEMPLATE = (
    Path(__file__).parent / "templates" / "com.opencomputer.profile-analyze.plist"
).read_text()
_PLIST_NAME = "com.opencomputer.profile-analyze.plist"
_LABEL = "com.opencomputer.profile-analyze"


class LaunchdUnsupportedError(RuntimeError):
    """Raised when launchd install is attempted on a non-macOS platform."""


def render_launchd_plist(*, executable: str, hour: int) -> str:
    """Render the plist body for the given parameters.

    The plist's ProgramArguments is hardcoded to
    ``["profile", "analyze", "run"]`` — these are the args the daily cron
    passes to ``opencomputer``. If a future caller needs different args,
    extend the template instead of threading them through the API.
    """
    from opencomputer.profiles import real_user_home
    log_path = str(real_user_home() / ".opencomputer" / "profile-analyze.log")
    return _TEMPLATE.format(
        executable=executable,
        hour=hour,
        log_path=log_path,
    )


def _launch_agents_dir() -> Path:
    """``~/Library/LaunchAgents`` — uses real_user_home for HOME-mutation immunity.

    Test suites monkeypatch this function directly to redirect the
    install location to a tmp_path.
    """
    from opencomputer.profiles import real_user_home
    return real_user_home() / "Library" / "LaunchAgents"


def _launchctl(*args: str) -> tuple[int, str, str]:
    """Run launchctl; return ``(rc, stdout, stderr)``.

    No-op-ish when launchctl isn't on PATH (e.g., running tests on Linux
    against macOS-conditional code) — returns rc=0 with a note in stderr.
    """
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


def install_launchd_plist(*, executable: str, hour: int = 9) -> Path:
    """Write the plist + bootstrap into the user's launchd domain.

    Daily cron at the given local hour (default 9am).
    """
    if sys.platform != "darwin":
        raise LaunchdUnsupportedError(
            f"launchd is macOS-only; got sys.platform={sys.platform!r}"
        )
    body = render_launchd_plist(executable=executable, hour=hour)
    target_dir = _launch_agents_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / _PLIST_NAME
    path.write_text(body)
    # Best-effort bootstrap into the user's GUI domain. Failures here
    # leave the plist on disk; the user can manually ``launchctl bootstrap``
    # later. Don't crash on bootstrap errors.
    uid = os.getuid()
    _launchctl("bootstrap", f"gui/{uid}", str(path))
    return path


def uninstall_launchd_plist() -> Path | None:
    """Bootout + remove the plist. Returns the removed path, or None if absent."""
    path = _launch_agents_dir() / _PLIST_NAME
    if not path.exists():
        return None
    uid = os.getuid()
    _launchctl("bootout", f"gui/{uid}/{_LABEL}")
    path.unlink()
    return path


def is_loaded() -> bool:
    """Best-effort: True iff ``launchctl print`` knows about the label."""
    if sys.platform != "darwin":
        return False
    uid = os.getuid()
    rc, _, _ = _launchctl("print", f"gui/{uid}/{_LABEL}")
    return rc == 0


__all__ = [
    "LaunchdUnsupportedError",
    "install_launchd_plist",
    "is_loaded",
    "render_launchd_plist",
    "uninstall_launchd_plist",
]
