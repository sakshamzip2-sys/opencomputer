"""Cross-platform Chrome binary detection.

Three detection strategies per OS, tried in order:

  macOS    1) launchservices plist for default URL handler
           2) osascript path-to-application bridging
           3) Hardcoded /Applications + ~/Applications candidate list
  Linux    1) xdg-settings + xdg-mime + .desktop Exec= parsing
           2) `which` lookup of common binary names
           3) Hardcoded /usr/bin + /snap/bin candidate list
  Windows  1) winreg HKCU UrlAssociations + HKCR shell\\open\\command
           2) Hardcoded %ProgramFiles%, %ProgramFiles(x86)%, %LOCALAPPDATA%

`read_browser_version` and `parse_browser_major_version` extract the binary's
declared version. The major number gates `existing-session` driver
(Chromium >= 144 required for Chrome MCP).
"""

from __future__ import annotations

import logging
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

_log = logging.getLogger("opencomputer.browser_control.chrome.executables")

# ─── per-platform tables ──────────────────────────────────────────────

CHROMIUM_EXE_NAMES: tuple[str, ...] = (
    "google-chrome",
    "google-chrome-stable",
    "google-chrome-beta",
    "brave-browser",
    "brave-browser-stable",
    "microsoft-edge",
    "microsoft-edge-stable",
    "chromium",
    "chromium-browser",
    "chrome",
)

CHROMIUM_BUNDLE_IDS: tuple[str, ...] = (
    "com.google.chrome",
    "com.google.chrome.canary",
    "com.brave.browser",
    "com.microsoft.edgemac",
    "org.chromium.chromium",
)

CHROMIUM_DESKTOP_IDS: tuple[str, ...] = (
    "google-chrome.desktop",
    "google-chrome-stable.desktop",
    "google-chrome-beta.desktop",
    "brave-browser.desktop",
    "microsoft-edge.desktop",
    "chromium.desktop",
    "chromium-browser.desktop",
)

_MAC_HARDCODED: tuple[str, ...] = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)

_LINUX_HARDCODED: tuple[str, ...] = (
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chrome",
    "/usr/bin/brave-browser",
    "/usr/bin/brave-browser-stable",
    "/usr/bin/brave",
    "/usr/bin/microsoft-edge",
    "/usr/bin/microsoft-edge-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/brave",
    "/snap/bin/chromium",
)

_WIN_HARDCODED_RELATIVE: tuple[str, ...] = (
    r"Google\Chrome\Application\chrome.exe",
    r"Google\Chrome SxS\Application\chrome.exe",
    r"BraveSoftware\Brave-Browser\Application\brave.exe",
    r"Microsoft\Edge\Application\msedge.exe",
    r"Chromium\Application\chrome.exe",
)


# ─── public API ───────────────────────────────────────────────────────


def resolve_chrome_executable(platform: str | None = None) -> str | None:
    """Return the path of a usable Chromium-flavor binary, or None.

    Tries the native default-browser query first, then falls back to a
    hardcoded list. `platform` defaults to `sys.platform`; pass explicitly
    in tests for cross-platform mocking.
    """
    plat = platform or sys.platform
    if plat == "darwin":
        for fn in (_detect_default_chrome_mac, _scan_hardcoded_paths_mac):
            found = fn()
            if found:
                return found
    elif plat == "win32":
        for fn in (_detect_default_chrome_windows, _scan_hardcoded_paths_windows):
            found = fn()
            if found:
                return found
    else:  # linux + everything else: try Linux strategy
        for fn in (_detect_default_chrome_linux, _scan_hardcoded_paths_linux):
            found = fn()
            if found:
                return found
    return None


_VERSION_RE = re.compile(r"(\d+(?:\.\d+){1,3})")


def read_browser_version(path: str | os.PathLike[str]) -> str | None:
    """Run `<path> --version` (2s timeout) and return stripped stdout, or None."""
    try:
        result = subprocess.run(  # noqa: S603 — argv is fully controlled
            [os.fspath(path), "--version"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired, OSError) as exc:
        _log.debug("read_browser_version(%s) failed: %s", path, exc)
        return None
    out = (result.stdout or "").strip()
    return out or None


def parse_browser_major_version(raw_version: str) -> int | None:
    """Extract the major version. Picks the LAST dotted version token in the string.

    `Chromium 3.0/1.2.3` -> 1 (mirrors OpenClaw's behavior; tested in their suite).
    """
    if not isinstance(raw_version, str):
        return None
    matches = _VERSION_RE.findall(raw_version)
    if not matches:
        return None
    last = matches[-1]
    head = last.split(".", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


# ─── macOS detection ──────────────────────────────────────────────────


_MAC_LSPLIST = (
    Path.home()
    / "Library"
    / "Preferences"
    / "com.apple.LaunchServices"
    / "com.apple.launchservices.secure.plist"
)


def _detect_default_chrome_mac() -> str | None:
    bundle_id = _read_default_http_bundle_id_mac()
    if not bundle_id:
        return None
    if bundle_id.lower() not in CHROMIUM_BUNDLE_IDS:
        return None
    return _resolve_app_path_via_osascript(bundle_id)


def _read_default_http_bundle_id_mac() -> str | None:
    try:
        data = plistlib.loads(_MAC_LSPLIST.read_bytes())
    except (FileNotFoundError, PermissionError, OSError, plistlib.InvalidFileException):
        return None
    handlers = data.get("LSHandlers")
    if not isinstance(handlers, list):
        return None
    # Prefer http, fallback to https.
    for scheme in ("http", "https"):
        for entry in handlers:
            if not isinstance(entry, dict):
                continue
            if entry.get("LSHandlerURLScheme") != scheme:
                continue
            role = entry.get("LSHandlerRoleAll") or entry.get("LSHandlerRoleViewer")
            if isinstance(role, str) and role:
                return role
    return None


def _resolve_app_path_via_osascript(bundle_id: str) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603 — argv is fully controlled
            [
                "/usr/bin/osascript",
                "-e",
                f'POSIX path of (path to application id "{bundle_id}")',
            ],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired, OSError):
        return None
    app_path = (result.stdout or "").strip()
    if not app_path:
        return None
    # app_path looks like /Applications/Google Chrome.app/. Find the binary in MacOS/.
    macos_dir = Path(app_path) / "Contents" / "MacOS"
    if not macos_dir.is_dir():
        return None
    info_plist = Path(app_path) / "Contents" / "Info.plist"
    exe_name: str | None = None
    if info_plist.is_file():
        try:
            info = plistlib.loads(info_plist.read_bytes())
            raw = info.get("CFBundleExecutable")
            if isinstance(raw, str) and raw:
                exe_name = raw
        except (OSError, plistlib.InvalidFileException):
            exe_name = None
    if exe_name:
        candidate = macos_dir / exe_name
        if candidate.is_file():
            return str(candidate)
    # Fallback: pick the first executable inside MacOS/.
    for child in macos_dir.iterdir():
        if child.is_file() and os.access(child, os.X_OK):
            return str(child)
    return None


def _scan_hardcoded_paths_mac() -> str | None:
    home = Path.home()
    candidates = list(_MAC_HARDCODED) + [str(home / p.lstrip("/")) for p in _MAC_HARDCODED]
    for path in candidates:
        if Path(path).is_file():
            return path
    return None


# ─── Linux detection ──────────────────────────────────────────────────


def _detect_default_chrome_linux() -> str | None:
    desktop_id = _query_xdg_default_browser_linux()
    if not desktop_id:
        return None
    if desktop_id.lower() not in CHROMIUM_DESKTOP_IDS:
        return None
    return _parse_desktop_exec_linux(desktop_id)


def _query_xdg_default_browser_linux() -> str | None:
    for argv in (
        ["xdg-settings", "get", "default-web-browser"],
        ["xdg-mime", "query", "default", "x-scheme-handler/http"],
    ):
        try:
            result = subprocess.run(  # noqa: S603
                argv,
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
        except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired, OSError):
            continue
        out = (result.stdout or "").strip()
        if out:
            return out
    return None


_LINUX_DESKTOP_DIRS = (
    Path.home() / ".local" / "share" / "applications",
    Path("/usr/local/share/applications"),
    Path("/usr/share/applications"),
    Path("/var/lib/snapd/desktop/applications"),
)


def _parse_desktop_exec_linux(desktop_id: str) -> str | None:
    for base in _LINUX_DESKTOP_DIRS:
        candidate = base / desktop_id
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeError):
            continue
        for line in text.splitlines():
            if not line.startswith("Exec="):
                continue
            cmd = line[len("Exec="):].strip()
            cmd = re.sub(r"%[fFuUickdDnNvm]", "", cmd).strip()
            try:
                tokens = shlex.split(cmd)
            except ValueError:
                continue
            if not tokens:
                continue
            head = tokens[0]
            if os.path.isabs(head):
                if Path(head).is_file():
                    return head
                continue
            located = shutil.which(head)
            if located and Path(located).name in CHROMIUM_EXE_NAMES:
                return located
    return None


def _scan_hardcoded_paths_linux() -> str | None:
    for path in _LINUX_HARDCODED:
        if Path(path).is_file():
            return path
    for name in CHROMIUM_EXE_NAMES:
        located = shutil.which(name)
        if located:
            return located
    return None


# ─── Windows detection ────────────────────────────────────────────────


def _detect_default_chrome_windows() -> str | None:
    try:
        import winreg  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return None
    return _detect_default_chrome_windows_impl()


def _detect_default_chrome_windows_impl() -> str | None:  # pragma: no cover - exercised on Windows only
    import winreg  # type: ignore[import-not-found]

    try:
        with winreg.OpenKey(  # type: ignore[attr-defined]
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice",
        ) as key:
            prog_id, _ = winreg.QueryValueEx(key, "ProgId")  # type: ignore[attr-defined]
    except OSError:
        return None
    if not isinstance(prog_id, str) or not prog_id:
        return None
    try:
        with winreg.OpenKey(  # type: ignore[attr-defined]
            winreg.HKEY_CLASSES_ROOT,
            rf"{prog_id}\shell\open\command",
        ) as key:
            command, _ = winreg.QueryValueEx(key, "")  # type: ignore[attr-defined]
    except OSError:
        return None
    if not isinstance(command, str) or not command:
        return None
    expanded = os.path.expandvars(command)
    match = re.search(r'"([^"]+\.exe)"', expanded) or re.search(r"(\S+\.exe)", expanded)
    if not match:
        return None
    exe_path = match.group(1)
    base = os.path.basename(exe_path).lower()
    if base.removesuffix(".exe") not in CHROMIUM_EXE_NAMES and base not in (
        "chrome.exe",
        "msedge.exe",
        "brave.exe",
    ):
        return None
    return exe_path if Path(exe_path).is_file() else None


def _scan_hardcoded_paths_windows() -> str | None:
    roots: list[str] = []
    for env_key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        val = os.environ.get(env_key)
        if val:
            roots.append(val)
    for root in roots:
        for rel in _WIN_HARDCODED_RELATIVE:
            candidate = os.path.join(root, rel)
            if os.path.isfile(candidate):
                return candidate
    return None
