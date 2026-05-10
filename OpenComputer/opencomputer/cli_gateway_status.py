"""Gateway runtime-status snapshot.

Composes systemd / launchd / schtasks probes + manual-PID detection
into a single ``GatewayRuntimeSnapshot`` dataclass that the
``oc gateway status`` command renders.

This is a port of Hermes ``hermes_cli/gateway.py:_get_service_pids``
+ ``gateway/status.py`` semantics. The implementation keeps every
subprocess and psutil call mocked-friendly for platform-independent
testing.

Manager values:
    "systemd-user"   — Linux user-scope service unit found
    "systemd-system" — Linux system-scope service unit found
    "launchd"        — macOS LaunchAgent registered
    "schtasks"       — Windows scheduled task registered
    "none"           — unknown OS / no service backend available
"""
from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

try:
    import psutil  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - psutil is a hard dep elsewhere
    psutil = None  # type: ignore[assignment]

from opencomputer.service._naming import service_label

_SUBPROCESS_TIMEOUT = 5.0
_LINUX_SCOPES: tuple[tuple[list[str], str], ...] = (
    (["systemctl", "--user"], "user"),
    (["systemctl"], "system"),
)


@dataclass(frozen=True)
class ProfileGatewayProcess:
    """A gateway process belonging to a different ``OPENCOMPUTER_HOME``."""

    profile: str
    home: Path
    pid: int


@dataclass(frozen=True)
class GatewayRuntimeSnapshot:
    """Composite view of the gateway across service backend + manual PIDs."""

    manager: str
    service_installed: bool = False
    service_running: bool = False
    main_pid: int | None = None
    gateway_pids: tuple[int, ...] = ()
    foreign_home_pids: tuple[ProfileGatewayProcess, ...] = ()
    service_scope: str | None = None

    @property
    def running(self) -> bool:
        """True if either the service or any manual PIDs are active."""
        return self.service_running or bool(self.gateway_pids)

    @property
    def has_process_service_mismatch(self) -> bool:
        """Service is installed and processes are running, yet the service
        itself reports inactive — strong signal that someone started the
        gateway by hand while a service unit also exists.
        """
        return (
            self.service_installed
            and self.running
            and not self.service_running
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_gateway_runtime_snapshot(profile: str = "default") -> GatewayRuntimeSnapshot:
    """Probe the active service manager + manual PIDs and return a snapshot."""
    label = service_label(profile)
    sysname = platform.system()
    manual_pids = _pgrep_pids(label, profile)
    foreign_pids = _foreign_home_pids(profile)

    if sysname == "Linux":
        return _linux_snapshot(label, manual_pids, foreign_pids)
    if sysname == "Darwin":
        return _macos_snapshot(label, manual_pids, foreign_pids)
    if sysname == "Windows":
        return _windows_snapshot(label, manual_pids, foreign_pids)

    return GatewayRuntimeSnapshot(
        manager="none",
        gateway_pids=manual_pids,
        foreign_home_pids=foreign_pids,
    )


# ---------------------------------------------------------------------------
# Linux — systemd (user, falling through to system)
# ---------------------------------------------------------------------------


def _linux_snapshot(
    label: str,
    manual_pids: tuple[int, ...],
    foreign_pids: tuple[ProfileGatewayProcess, ...],
) -> GatewayRuntimeSnapshot:
    for scope_args, scope_name in _LINUX_SCOPES:
        units = _systemctl_list_units(scope_args, label)
        if not units:
            continue
        installed = True
        running = False
        main_pid: int | None = None
        for unit_name, active_state in units:
            unit_active = active_state == "active"
            if unit_active:
                running = True
            pid = _systemctl_show_main_pid(scope_args, unit_name)
            if pid is not None and unit_active:
                main_pid = pid
        manager = "systemd-user" if scope_name == "user" else "systemd-system"
        return GatewayRuntimeSnapshot(
            manager=manager,
            service_installed=installed,
            service_running=running,
            main_pid=main_pid,
            gateway_pids=manual_pids,
            foreign_home_pids=foreign_pids,
            service_scope=scope_name,
        )

    # No unit found in either scope.
    return GatewayRuntimeSnapshot(
        manager="systemd-user",
        service_installed=False,
        service_running=False,
        main_pid=None,
        gateway_pids=manual_pids,
        foreign_home_pids=foreign_pids,
        service_scope=None,
    )


def _systemctl_list_units(
    scope_args: list[str], label: str
) -> list[tuple[str, str]]:
    """Return ``(unit_name, active_state)`` pairs (empty if none / on error).

    ``systemctl list-units --plain --no-legend --no-pager`` columns:
        UNIT  LOAD  ACTIVE  SUB  DESCRIPTION

    We extract column 0 (unit name) + column 2 (ACTIVE).
    """
    try:
        result = subprocess.run(
            scope_args
            + [
                "list-units",
                f"{label}*",
                "--plain",
                "--no-legend",
                "--no-pager",
            ],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []

    if result.returncode != 0:
        return []

    units: list[tuple[str, str]] = []
    for line in result.stdout.strip().splitlines():
        parts = line.split()
        if not parts:
            continue
        unit_name = parts[0]
        if not unit_name.endswith(".service"):
            continue
        active_state = parts[2] if len(parts) >= 3 else ""
        units.append((unit_name, active_state))
    return units


def _systemctl_show_main_pid(scope_args: list[str], unit: str) -> int | None:
    """Run ``systemctl show <unit> --property=MainPID --value`` and parse PID."""
    try:
        result = subprocess.run(
            scope_args + ["show", unit, "--property=MainPID", "--value"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None

    if result.returncode != 0:
        return None

    raw = result.stdout.strip()
    try:
        parsed = int(raw)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


# ---------------------------------------------------------------------------
# macOS — launchctl
# ---------------------------------------------------------------------------


def _launchd_label_for(label: str) -> str:
    """Translate the systemd-style label to the launchd reverse-DNS label.

    ``opencomputer-gateway`` -> ``com.opencomputer.gateway``
    ``opencomputer-gateway-abc12345`` -> ``com.opencomputer.gateway.abc12345``
    """
    return "com." + label.replace("-", ".")


def _macos_snapshot(
    label: str,
    manual_pids: tuple[int, ...],
    foreign_pids: tuple[ProfileGatewayProcess, ...],
) -> GatewayRuntimeSnapshot:
    plist_label = _launchd_label_for(label)
    try:
        result = subprocess.run(
            ["launchctl", "list", plist_label],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return GatewayRuntimeSnapshot(
            manager="launchd",
            service_installed=False,
            service_running=False,
            gateway_pids=manual_pids,
            foreign_home_pids=foreign_pids,
        )

    if result.returncode != 0:
        return GatewayRuntimeSnapshot(
            manager="launchd",
            service_installed=False,
            service_running=False,
            gateway_pids=manual_pids,
            foreign_home_pids=foreign_pids,
        )

    main_pid = _parse_launchctl_pid(result.stdout)
    return GatewayRuntimeSnapshot(
        manager="launchd",
        service_installed=True,
        service_running=main_pid is not None and main_pid > 0,
        main_pid=main_pid,
        gateway_pids=manual_pids,
        foreign_home_pids=foreign_pids,
    )


def _parse_launchctl_pid(stdout: str) -> int | None:
    """Extract PID from launchctl-list output. Two formats supported:

    1. Modern dict form: ``"PID" = 1234;``
    2. Older tabular form: ``1234\tStatus\tLabel``
    """
    # Format 1: ``"PID" = 1234;``
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith('"PID"') or stripped.startswith("PID"):
            # Match ``"PID" = 1234;`` or ``PID = 1234;``
            for token in stripped.replace(";", " ").replace("=", " ").split():
                try:
                    pid = int(token)
                    if pid > 0:
                        return pid
                except ValueError:
                    continue
    # Format 2: tabular
    for line in stdout.strip().splitlines():
        parts = line.split()
        if len(parts) >= 1 and parts[0].lstrip("-").isdigit():
            try:
                pid = int(parts[0])
                if pid > 0:
                    return pid
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------------------
# Windows — schtasks
# ---------------------------------------------------------------------------


def _windows_snapshot(
    label: str,
    manual_pids: tuple[int, ...],
    foreign_pids: tuple[ProfileGatewayProcess, ...],
) -> GatewayRuntimeSnapshot:
    try:
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", label, "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return GatewayRuntimeSnapshot(
            manager="schtasks",
            service_installed=False,
            service_running=False,
            gateway_pids=manual_pids,
            foreign_home_pids=foreign_pids,
        )

    if result.returncode != 0:
        return GatewayRuntimeSnapshot(
            manager="schtasks",
            service_installed=False,
            service_running=False,
            gateway_pids=manual_pids,
            foreign_home_pids=foreign_pids,
        )

    rows = [r for r in result.stdout.strip().splitlines() if r]
    installed = bool(rows)
    running = False
    if rows:
        # CSV: "<TaskName>","<NextRun>","<Status>"
        first = rows[0]
        # Status is the last quoted token
        status_token = first.rsplit(",", 1)[-1].strip().strip('"').lower()
        running = status_token == "running"

    return GatewayRuntimeSnapshot(
        manager="schtasks",
        service_installed=installed,
        service_running=running,
        gateway_pids=manual_pids,
        foreign_home_pids=foreign_pids,
    )


# ---------------------------------------------------------------------------
# Manual PIDs — POSIX pgrep / Windows wmic
# ---------------------------------------------------------------------------


def _pgrep_pids(label: str, profile: str) -> tuple[int, ...]:
    """Find manual gateway PIDs using ``pgrep -af`` (POSIX) or wmic (Windows).

    Filters out the daemon's own PID.
    """
    own_pid = os.getpid()
    sysname = platform.system()
    if sysname == "Windows":
        return _wmic_pids(own_pid)

    try:
        result = subprocess.run(
            ["pgrep", "-af", "opencomputer.*gateway|oc.*gateway"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ()

    # pgrep exits 1 when no matches found — that is a normal case, not an error
    if result.returncode not in (0, 1):
        return ()

    pids: list[int] = []
    for line in result.stdout.strip().splitlines():
        parts = line.split(maxsplit=1)
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == own_pid or pid <= 0:
            continue
        pids.append(pid)
    return tuple(pids)


def _wmic_pids(own_pid: int) -> tuple[int, ...]:
    """Best-effort Windows process scan. Falls back to () on any error."""
    try:
        result = subprocess.run(
            [
                "wmic",
                "process",
                "where",
                "CommandLine like '%opencomputer%gateway%'",
                "get",
                "ProcessId",
                "/format:csv",
            ],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ()

    if result.returncode != 0:
        return ()

    pids: list[int] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(",")
        if not parts:
            continue
        last = parts[-1].strip()
        if not last.isdigit():
            continue
        pid = int(last)
        if pid != own_pid and pid > 0:
            pids.append(pid)
    return tuple(pids)


# ---------------------------------------------------------------------------
# Foreign-home detection via psutil
# ---------------------------------------------------------------------------


def _resolve_home() -> str:
    """Return the resolved active ``OPENCOMPUTER_HOME``."""
    return os.environ.get("OPENCOMPUTER_HOME") or str(Path.home() / ".opencomputer")


def _foreign_home_pids(profile: str) -> tuple[ProfileGatewayProcess, ...]:
    """Return gateway processes whose ``OPENCOMPUTER_HOME`` differs from ours."""
    if psutil is None:
        return ()

    own_home = Path(_resolve_home()).resolve()
    own_home_str = str(own_home)
    out: list[ProfileGatewayProcess] = []

    try:
        proc_iter = psutil.process_iter(["pid", "cmdline", "environ"])
    except Exception:
        return ()

    for proc in proc_iter:
        try:
            info = proc.info  # type: ignore[union-attr]
        except Exception:
            continue
        cmdline = info.get("cmdline") or []
        if not isinstance(cmdline, (list, tuple)):
            continue
        joined = " ".join(str(p) for p in cmdline)
        if "gateway" not in joined:
            continue
        environ = info.get("environ") or {}
        if not isinstance(environ, dict):
            continue
        their_home = environ.get("OPENCOMPUTER_HOME")
        if not their_home or their_home == own_home_str:
            continue
        try:
            their_home_resolved = Path(their_home).resolve()
        except (OSError, RuntimeError):
            their_home_resolved = Path(their_home)
        if str(their_home_resolved) == own_home_str:
            continue
        try:
            pid = int(info.get("pid"))
        except (TypeError, ValueError):
            continue
        out.append(
            ProfileGatewayProcess(
                profile=profile,
                home=their_home_resolved,
                pid=pid,
            )
        )

    return tuple(out)


__all__ = [
    "GatewayRuntimeSnapshot",
    "ProfileGatewayProcess",
    "get_gateway_runtime_snapshot",
]


# Suppress unused-import warning for ``field`` (kept for forward-compat
# in case the dataclass grows mutable defaults).
_ = field
