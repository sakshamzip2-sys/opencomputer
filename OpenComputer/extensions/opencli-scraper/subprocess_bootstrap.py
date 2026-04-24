"""Binary detection for OpenCLI and Chrome/Chromium.

``detect_opencli()`` and ``detect_chrome()`` locate the required executables
without auto-installing anything. If a binary is missing, ``BootstrapError``
is raised with platform-specific install instructions.

Design doc §11: No auto-install. Surface requirements explicitly so the user
provides the binaries; silent installs are a security risk.

OpenCLI version pinning: The minimum supported version is defined in
``wrapper.MIN_OPENCLI_VERSION``. subprocess_bootstrap does not re-check the
version — that is ``OpenCLIWrapper._check_version()``'s responsibility.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


class BootstrapError(RuntimeError):
    """Raised when a required binary is not found on the system."""


# ── Common Chrome/Chromium locations by platform ────────────────────────────────

_CHROME_PATHS_MACOS = [
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
    Path("/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"),
]

_CHROME_NAMES_LINUX = [
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
]

_CHROME_PATHS_WINDOWS = [
    Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
    Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
    Path("C:/Program Files/Chromium/Application/chrome.exe"),
]


# ── OpenCLI detection ──────────────────────────────────────────────────────────


def detect_opencli() -> Path | None:
    """Detect the ``opencli`` binary.

    Tries (in order):
    1. ``shutil.which("opencli")`` — global install via ``npm install -g``.
    2. ``npx --no-install opencli --version`` — locally installed via npx.

    Returns
    -------
    Path | None
        The resolved binary path, or ``None`` if not found at either location.
        Callers should raise ``BootstrapError`` when ``None`` is returned.
    """
    # 1. Global install.
    found = shutil.which("opencli")
    if found:
        log.debug("detect_opencli: found global binary at %r", found)
        return Path(found)

    # 2. npx fallback — verify the command works without triggering an install.
    npx = shutil.which("npx")
    if npx:
        try:
            result = subprocess.run(
                [npx, "--no-install", "opencli", "--version"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                log.debug("detect_opencli: found via npx, version=%r", result.stdout.strip())
                return Path(npx)  # Return npx path; caller prepends "npx opencli"
        except (subprocess.TimeoutExpired, OSError) as exc:
            log.debug("detect_opencli: npx probe failed: %s", exc)

    log.debug("detect_opencli: binary not found")
    return None


def require_opencli() -> Path:
    """Like ``detect_opencli()`` but raises ``BootstrapError`` if not found."""
    binary = detect_opencli()
    if binary is not None:
        return binary
    raise BootstrapError(
        "opencli binary not found. Install it with:\n\n"
        "    npm install -g @jackwener/opencli\n\n"
        "Requires Node.js >= 18. Verify with: node --version\n"
        "Full docs: https://github.com/jackwener/opencli"
    )


# ── Chrome/Chromium detection ──────────────────────────────────────────────────


def detect_chrome() -> Path | None:
    """Detect a Chrome or Chromium binary.

    Searches platform-specific paths and ``PATH``.

    Returns
    -------
    Path | None
        Absolute path to the binary, or ``None`` if not found.
    """
    system = platform.system()

    if system == "Darwin":  # macOS
        for p in _CHROME_PATHS_MACOS:
            if p.exists():
                log.debug("detect_chrome: found at %r", p)
                return p

    elif system == "Linux":
        for name in _CHROME_NAMES_LINUX:
            found = shutil.which(name)
            if found:
                log.debug("detect_chrome: found %r at %r", name, found)
                return Path(found)

    elif system == "Windows":
        for p in _CHROME_PATHS_WINDOWS:
            if p.exists():
                log.debug("detect_chrome: found at %r", p)
                return p

    # Cross-platform PATH fallback.
    for name in ("google-chrome", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            log.debug("detect_chrome: found %r via PATH", found)
            return Path(found)

    log.debug("detect_chrome: browser not found")
    return None


def require_chrome() -> Path:
    """Like ``detect_chrome()`` but raises ``BootstrapError`` if not found."""
    binary = detect_chrome()
    if binary is not None:
        return binary

    system = platform.system()
    if system == "Darwin":
        install_hint = (
            "Download from: https://www.google.com/chrome/\n"
            "Or install Chromium via Homebrew: brew install --cask chromium"
        )
    elif system == "Linux":
        install_hint = (
            "Install Chromium: sudo apt install chromium-browser\n"
            "Or Google Chrome: https://www.google.com/chrome/"
        )
    else:
        install_hint = "Download from: https://www.google.com/chrome/"

    raise BootstrapError(
        "Chrome or Chromium not found. OpenCLI requires it for browser automation.\n\n"
        f"{install_hint}"
    )


# ── Async detection helpers ────────────────────────────────────────────────────


async def detect_opencli_async() -> Path | None:
    """Async wrapper around ``detect_opencli()`` — runs in thread pool."""
    return await asyncio.to_thread(detect_opencli)


async def detect_chrome_async() -> Path | None:
    """Async wrapper around ``detect_chrome()`` — runs in thread pool."""
    return await asyncio.to_thread(detect_chrome)


__all__ = [
    "BootstrapError",
    "detect_opencli",
    "detect_chrome",
    "require_opencli",
    "require_chrome",
    "detect_opencli_async",
    "detect_chrome_async",
]
