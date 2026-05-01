"""systemd-user service install/uninstall.

systemd is Linux-only. macOS uses launchd (out of scope for now). Windows
uses the Service Control Manager (also out of scope). Both can be added
as sibling modules later.

The unit installs into the standard XDG location:
``$XDG_CONFIG_HOME/systemd/user/opencomputer.service`` (defaults to
``~/.config/systemd/user/opencomputer.service``). After install, this
module runs ``systemctl --user daemon-reload`` automatically; the user
runs ``systemctl --user enable --now opencomputer`` to start the agent.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_TEMPLATE = (Path(__file__).parent / "templates" / "opencomputer.service").read_text()


class ServiceUnsupportedError(RuntimeError):
    """Raised when service install is attempted on a non-systemd platform."""


def render_systemd_unit(
    *, executable: str, workdir: str | Path, profile: str, extra_args: str
) -> str:
    """Render the systemd unit body for the given parameters."""
    return _TEMPLATE.format(
        executable=executable,
        workdir=str(workdir),
        profile=profile,
        extra_args=extra_args,
    )


def _user_unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user" / "opencomputer.service"


def _systemctl(*args: str) -> tuple[int, str, str]:
    """Call ``systemctl --user``; return (rc, stdout, stderr).

    No-op-ish (returns rc=0 with a note in stderr) when systemctl isn't
    on PATH so install/uninstall on a Linux box without systemd doesn't
    crash — just leaves the unit file in place / removed.
    """
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


def install_systemd_unit(
    *, executable: str, workdir: str | Path, profile: str, extra_args: str
) -> Path:
    """Write the unit file and run ``daemon-reload``. Returns the path written."""
    if not sys.platform.startswith("linux"):
        raise ServiceUnsupportedError(
            f"systemd is Linux-only; got sys.platform={sys.platform!r}"
        )
    body = render_systemd_unit(
        executable=executable, workdir=workdir,
        profile=profile, extra_args=extra_args,
    )
    path = _user_unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    _systemctl("daemon-reload")
    return path


def uninstall_systemd_unit() -> Path | None:
    """Stop + disable + remove the unit. Returns the removed path, or None if absent."""
    path = _user_unit_path()
    if not path.exists():
        return None
    _systemctl("stop", "opencomputer.service")
    _systemctl("disable", "opencomputer.service")
    path.unlink()
    _systemctl("daemon-reload")
    return path


def is_active() -> bool:
    """Return True if the systemd unit reports active."""
    rc, out, _ = _systemctl("is-active", "opencomputer.service")
    return rc == 0 and out.strip() == "active"


# ─── Plan 3 (2026-05-01): profile-analyze daily timer + service ───────


_PA_TIMER_TEMPLATE = (
    Path(__file__).parent / "templates" / "opencomputer-profile-analyze.timer"
).read_text()
_PA_SERVICE_TEMPLATE = (
    Path(__file__).parent / "templates" / "opencomputer-profile-analyze.service"
).read_text()
_PA_UNIT_NAME = "opencomputer-profile-analyze"


def _pa_unit_dir() -> Path:
    """systemd-user XDG-honoring unit directory."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def install_profile_analyze_timer(*, executable: str) -> tuple[Path, Path]:
    """Install the daily profile-analyze systemd timer + service.

    Plan 3 of 3 — Linux side of ``oc profile analyze install``. macOS
    uses launchd via ``service/launchd.py``. Returns (timer_path, service_path).
    """
    if not sys.platform.startswith("linux"):
        raise ServiceUnsupportedError(
            f"systemd is Linux-only; got sys.platform={sys.platform!r}"
        )
    from opencomputer.profiles import real_user_home
    log_path = str(real_user_home() / ".opencomputer" / "profile-analyze.log")
    target_dir = _pa_unit_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    timer_path = target_dir / f"{_PA_UNIT_NAME}.timer"
    service_path = target_dir / f"{_PA_UNIT_NAME}.service"
    timer_path.write_text(_PA_TIMER_TEMPLATE)
    service_path.write_text(
        _PA_SERVICE_TEMPLATE.format(executable=executable, log_path=log_path),
    )
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", f"{_PA_UNIT_NAME}.timer")
    return (timer_path, service_path)


def uninstall_profile_analyze_timer() -> tuple[Path | None, Path | None]:
    """Stop + disable + remove the profile-analyze timer + service.

    Returns (removed_timer_path, removed_service_path) — either may be
    None if the file wasn't present.
    """
    target_dir = _pa_unit_dir()
    timer_path = target_dir / f"{_PA_UNIT_NAME}.timer"
    service_path = target_dir / f"{_PA_UNIT_NAME}.service"
    timer_existed = timer_path.exists()
    service_existed = service_path.exists()
    _systemctl("stop", f"{_PA_UNIT_NAME}.timer")
    _systemctl("disable", f"{_PA_UNIT_NAME}.timer")
    if timer_existed:
        timer_path.unlink()
    if service_existed:
        service_path.unlink()
    _systemctl("daemon-reload")
    return (
        timer_path if timer_existed else None,
        service_path if service_existed else None,
    )


def is_profile_analyze_timer_active() -> bool:
    """Return True if the daily profile-analyze timer is active on systemd."""
    if not sys.platform.startswith("linux"):
        return False
    rc, out, _ = _systemctl("is-active", f"{_PA_UNIT_NAME}.timer")
    return rc == 0 and out.strip() == "active"


__all__ = [
    "ServiceUnsupportedError",
    "install_profile_analyze_timer",
    "install_systemd_unit",
    "is_active",
    "is_profile_analyze_timer_active",
    "render_systemd_unit",
    "uninstall_profile_analyze_timer",
    "uninstall_systemd_unit",
]
