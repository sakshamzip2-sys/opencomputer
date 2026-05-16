"""Host-environment fingerprint — startup host profile for plugins + prompt.

At startup OpenComputer only knows cwd / home / OS-name / time. That is
not enough for plugins that must adapt to the host: a Linux computer-use
backend needs to know whether the display server is Wayland or X11 (to
pick ``ydotool`` vs ``xdotool``), whether the box is headless, whether it
is running inside a container, and so on. The agent's own system prompt
benefits from the same fingerprint (architecture, OS pretty-name, CPU,
RAM).

:class:`HostProfile` is a frozen + slotted snapshot of that fingerprint.
:func:`detect_host` runs every probe — each individually wrapped so a
single failing probe degrades only its own field to a safe default and
never raises. The result is process-cached: the host does not change
within a process lifetime.

This module imports STDLIB + ``psutil`` ONLY. It must never import from
``opencomputer.*`` — see ``plugin_sdk/CLAUDE.md`` hard rule #1, enforced
by ``tests/test_phase6a.py``.
"""

from __future__ import annotations

import functools
import os
import platform
import socket
from dataclasses import dataclass

import psutil

# ─── Safe-default sentinels ────────────────────────────────────────────
#: String fields degrade to this when their probe raises.
_UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class HostProfile:
    """Immutable snapshot of the host environment, captured at startup.

    Every field has a safe default so a partially-failed probe still
    yields a usable instance. Construct via :func:`detect_host` — never
    by hand in production code — so the cached singleton is shared.

    Display / container fields are the ones plugins lean on most:

    * ``display_server`` — ``"wayland"`` / ``"x11"`` / ``"aqua"`` /
      ``"windows"`` / ``"none"``. A Linux computer-use backend keys its
      ``xdotool`` vs ``ydotool`` choice off this.
    * ``is_headless`` — ``True`` on Linux with no display server. Lets a
      backend fail fast (or switch to a virtual framebuffer) instead of
      crashing on first screenshot.
    * ``is_container`` / ``is_wsl`` — sandbox / VM awareness; affects
      whether certain host-level operations are even meaningful.
    """

    #: ``platform.system()`` — "Darwin" / "Linux" / "Windows".
    os_name: str = _UNKNOWN
    #: Human label — "macOS 14.3" / "Ubuntu 22.04.4 LTS" / "Windows 10".
    os_pretty: str = _UNKNOWN
    #: Raw OS version string (``platform.version()``).
    os_version: str = _UNKNOWN
    #: CPU architecture — ``platform.machine()`` (e.g. "arm64", "x86_64").
    arch: str = _UNKNOWN
    #: Running interpreter version — ``platform.python_version()``.
    python_version: str = _UNKNOWN
    #: Logical CPU count (cores incl. SMT/hyperthreads).
    cpu_logical: int = 0
    #: Physical CPU core count (excludes SMT). 0 if undeterminable.
    cpu_physical: int = 0
    #: Total physical RAM in GiB, rounded to one decimal.
    total_ram_gb: float = 0.0
    #: ``socket.gethostname()``.
    hostname: str = _UNKNOWN
    #: Display server — "wayland" / "x11" / "aqua" / "windows" / "none".
    display_server: str = "none"
    #: ``True`` when there is no display server (Linux, no DISPLAY).
    is_headless: bool = False
    #: ``True`` when running inside a Docker / LXC / Kubernetes container.
    is_container: bool = False
    #: ``True`` when running under Windows Subsystem for Linux.
    is_wsl: bool = False

    def summary_line(self) -> str:
        """Return a compact one-line fingerprint for prompts / logs.

        Example::

            macOS 14.3 (arm64) · 10 CPU · 16.0 GiB · aqua

        Container / headless / WSL flags are appended only when set, so
        the common (bare-metal desktop) case stays terse.
        """
        parts = [
            f"{self.os_pretty} ({self.arch})",
            f"{self.cpu_logical} CPU",
            f"{self.total_ram_gb:g} GiB",
            self.display_server,
        ]
        flags = []
        if self.is_headless:
            flags.append("headless")
        if self.is_container:
            flags.append("container")
        if self.is_wsl:
            flags.append("wsl")
        if flags:
            parts.append("[" + ", ".join(flags) + "]")
        return " · ".join(parts)


# ─── Individual probes ─────────────────────────────────────────────────
# Each probe is total: it returns a value or a safe default, never
# raises. ``detect_host`` additionally wraps every call in try/except as
# a belt-and-braces guard against unexpected platform edge cases.


def _probe_os_pretty(os_name: str) -> str:
    """Human-readable OS label, best-effort per platform."""
    if os_name == "Darwin":
        ver = platform.mac_ver()[0]
        return f"macOS {ver}".strip() if ver else "macOS"
    if os_name == "Linux":
        try:
            release = platform.freedesktop_os_release()
        except (OSError, AttributeError):
            # No /etc/os-release, or Python < 3.10 lacking the helper.
            release = {}
        pretty = release.get("PRETTY_NAME") or release.get("NAME")
        return pretty if pretty else "Linux"
    if os_name == "Windows":
        rel, ver, _csd, _ptype = platform.win32_ver()
        label = f"Windows {rel}".strip()
        return f"{label} ({ver})" if ver else (label or "Windows")
    return os_name or _UNKNOWN


def _probe_display_server(os_name: str) -> str:
    """Resolve the windowing system in use.

    macOS → ``aqua``; Windows → ``windows``. On Linux: ``WAYLAND_DISPLAY``
    wins, then ``XDG_SESSION_TYPE``, then a bare ``DISPLAY`` implies X11;
    nothing set → ``none`` (headless).
    """
    if os_name == "Darwin":
        return "aqua"
    if os_name == "Windows":
        return "windows"
    # Linux / other POSIX.
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    session_type = (os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()
    if session_type in ("wayland", "x11"):
        return session_type
    if os.environ.get("DISPLAY"):
        return "x11"
    return "none"


def _probe_is_container() -> bool:
    """Detect container runtimes via well-known marker files / cgroup."""
    for marker in ("/.dockerenv", "/run/.containerenv"):
        try:
            if os.path.exists(marker):
                return True
        except OSError:
            pass
    try:
        with open("/proc/1/cgroup", encoding="utf-8", errors="ignore") as fh:
            cgroup = fh.read()
        if any(tok in cgroup for tok in ("docker", "lxc", "kubepods")):
            return True
    except OSError:
        pass
    return False


def _probe_is_wsl() -> bool:
    """``True`` when the Linux kernel reports a Microsoft / WSL build."""
    try:
        with open("/proc/version", encoding="utf-8", errors="ignore") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


def _detect_host_uncached() -> HostProfile:
    """Run every probe and assemble a :class:`HostProfile`.

    Each probe is wrapped individually: a failure degrades only that
    field to its safe default. The whole function therefore never
    raises. Kept separate from the cached :func:`detect_host` so tests
    can exercise degradation without poisoning the process cache.
    """

    def _safe(fn, default):  # type: ignore[no-untyped-def]
        try:
            value = fn()
            return value if value is not None else default
        except Exception:
            return default

    os_name = _safe(lambda: platform.system() or _UNKNOWN, _UNKNOWN)
    os_pretty = _safe(lambda: _probe_os_pretty(os_name), os_name)
    os_version = _safe(lambda: platform.version() or _UNKNOWN, _UNKNOWN)
    arch = _safe(lambda: platform.machine() or _UNKNOWN, _UNKNOWN)
    python_version = _safe(lambda: platform.python_version(), _UNKNOWN)
    cpu_logical = _safe(lambda: psutil.cpu_count(logical=True) or 0, 0)
    cpu_physical = _safe(lambda: psutil.cpu_count(logical=False) or 0, 0)
    total_ram_gb = _safe(
        lambda: round(psutil.virtual_memory().total / 1024**3, 1), 0.0
    )
    hostname = _safe(lambda: socket.gethostname() or _UNKNOWN, _UNKNOWN)
    display_server = _safe(lambda: _probe_display_server(os_name), "none")
    is_container = _safe(_probe_is_container, False)
    is_wsl = _safe(_probe_is_wsl, False)

    return HostProfile(
        os_name=os_name,
        os_pretty=os_pretty,
        os_version=os_version,
        arch=arch,
        python_version=python_version,
        cpu_logical=cpu_logical,
        cpu_physical=cpu_physical,
        total_ram_gb=total_ram_gb,
        hostname=hostname,
        display_server=display_server,
        is_headless=(display_server == "none"),
        is_container=is_container,
        is_wsl=is_wsl,
    )


@functools.cache
def detect_host() -> HostProfile:
    """Return the process-cached :class:`HostProfile` fingerprint.

    The host does not change within a process lifetime, so the result
    is memoised — repeated calls return the *same* instance (cheap, and
    callers can use identity checks). Every probe is failure-isolated;
    this never raises.

    For tests that need to exercise probe degradation, call
    :func:`_detect_host_uncached` directly, or clear the cache via
    ``detect_host.cache_clear()``.
    """
    return _detect_host_uncached()


__all__ = ["HostProfile", "detect_host"]
