"""Launchd service installer section (S5).

Modeled after Hermes's `setup_gateway` launchd-install prompt
(hermes_cli/setup.py around line 2380, "Install the gateway as a
launchd service?"). Independently re-implemented (no code copied).

macOS-only: writes a `~/Library/LaunchAgents/com.opencomputer.gateway.plist`
that runs `oc gateway run` on login + RunAtLoad, then `launchctl load`s
it. On Linux / Windows the section is a no-op (logs and returns).

User can later remove the service with:
    launchctl unload ~/Library/LaunchAgents/com.opencomputer.gateway.plist
    rm ~/Library/LaunchAgents/com.opencomputer.gateway.plist
"""
from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path

from opencomputer.cli_setup.sections import SectionResult, WizardCtx
from opencomputer.cli_ui.menu import Choice, radiolist

_LABEL = "com.opencomputer.gateway"
_PLIST_FILENAME = f"{_LABEL}.plist"


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _oc_executable_path() -> str:
    """Locate the `oc` shim. Prefer PATH-resolved; fall back to a
    plausible Homebrew location if which-lookup fails."""
    found = shutil.which("oc")
    if found:
        return found
    # Last-resort fallback — common Homebrew location on Apple Silicon
    return "/opt/homebrew/bin/oc"


def _run_launchctl(args: list[str]) -> int:
    """Run `launchctl <args...>`. Returns the exit code; never raises."""
    try:
        return subprocess.run(  # noqa: S603 — args list is internal
            ["launchctl", *args],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
    except (FileNotFoundError, OSError):
        return -1


def _build_plist(oc_path: str, plist_label: str) -> str:
    """Generate the LaunchAgent plist XML for `oc gateway run`."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{plist_label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{oc_path}</string>
    <string>gateway</string>
    <string>run</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{Path.home()}/.opencomputer/logs/gateway.stdout.log</string>
  <key>StandardErrorPath</key>
  <string>{Path.home()}/.opencomputer/logs/gateway.stderr.log</string>
</dict>
</plist>
"""


def run_launchd_service_section(ctx: WizardCtx) -> SectionResult:
    if not _is_macos():
        print("  (launchd is macOS-only — skipped on this platform)")
        return SectionResult.SKIPPED_FRESH

    choices = [
        Choice("Install gateway as launchd service", "install"),
        Choice("Skip — run gateway manually with `oc gateway run`", "skip"),
    ]
    idx = radiolist(
        "Install the gateway as a launchd service? (runs in background, starts on login)",
        choices, default=0,
    )
    if idx == 1:
        return SectionResult.SKIPPED_FRESH

    target_dir = _launch_agents_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    plist_path = target_dir / _PLIST_FILENAME
    oc_path = _oc_executable_path()
    plist_path.write_text(_build_plist(oc_path, _LABEL))

    # If a previous version is loaded, unload it first (idempotent).
    _run_launchctl(["unload", str(plist_path)])
    rc = _run_launchctl(["load", str(plist_path)])
    if rc != 0:
        print(f"  ⚠ launchctl load returned {rc} — plist written; "
              "you may need to load it manually.")

    ctx.config.setdefault("gateway", {})
    ctx.config["gateway"]["launchd_installed"] = True

    print(f"  ✓ Installed gateway service at {plist_path}")
    print("    To remove later:")
    print(f"      launchctl unload {plist_path}")
    print(f"      rm {plist_path}")

    return SectionResult.CONFIGURED
