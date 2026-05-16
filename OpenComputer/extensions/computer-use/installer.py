"""cua-driver installer — ported from hermes-agent ``hermes_cli/tools_config.py``.

``cua-driver`` is an external binary from https://github.com/trycua/cua,
installed via a curl-piped shell script. macOS-only.

The upstream installer always pulls the latest release tag, so re-running it
is the canonical upgrade path. Two modes:

* ``upgrade=False`` — skip if already installed, install otherwise. Used by
  the toolset-enable / first-install flow where we don't want to surprise the
  user with a network fetch.
* ``upgrade=True`` — always re-run the installer. Used by ``oc computer-use
  install --upgrade``.

``install_cua_driver`` returns ``True`` iff cua-driver is installed (or
successfully refreshed) when the function returns. macOS-only — silently
returns ``False`` on other platforms in ``upgrade`` mode.
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess

logger = logging.getLogger("opencomputer.computer_use.installer")

#: The curl-piped upstream installer command.
INSTALL_CMD = (
    "/bin/bash -c \"$(curl -fsSL "
    "https://raw.githubusercontent.com/trycua/cua/main/"
    "libs/cua-driver/scripts/install.sh)\""
)


# Print helpers — kept as module functions so tests can patch them, mirroring
# the hermes ``_print_warning`` / ``_print_info`` / ``_print_success`` hooks.

def _print_info(msg: str) -> None:
    print(msg)


def _print_success(msg: str) -> None:
    print(msg)


def _print_warning(msg: str) -> None:
    print(msg)


def cua_driver_version() -> str:
    """Best-effort ``cua-driver --version``; empty string on any failure."""
    try:
        return subprocess.run(
            ["cua-driver", "--version"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return ""


def _run_cua_driver_installer(label: str = "Installing", verbose: bool = True) -> bool:
    """Run the upstream cua-driver install.sh. Returns True on success.

    The script is idempotent: it always downloads the latest release, so
    re-running it on an already-installed system performs an upgrade.
    """
    if verbose:
        _print_info(f"    {label} cua-driver (macOS background computer-use)...")
    else:
        _print_info(f"    {label} cua-driver...")
    try:
        result = subprocess.run(INSTALL_CMD, shell=True, timeout=300)
        if result.returncode == 0 and shutil.which("cua-driver"):
            if verbose:
                _print_success("    cua-driver installed.")
                _print_info("    IMPORTANT — grant macOS permissions now:")
                _print_info("      System Settings > Privacy & Security > Accessibility")
                _print_info("      System Settings > Privacy & Security > Screen Recording")
                _print_info("    Both must allow the terminal / OpenComputer process.")
            return True
        _print_warning(f"    cua-driver {label.lower()} did not complete. Re-run manually:")
        _print_info(f"      {INSTALL_CMD}")
        return False
    except subprocess.TimeoutExpired:
        _print_warning(f"    cua-driver {label.lower()} timed out. Re-run manually.")
        return False
    except Exception as e:
        _print_warning(f"    cua-driver {label.lower()} failed: {e}")
        return False


def install_cua_driver(upgrade: bool = False) -> bool:
    """Install or refresh the cua-driver binary used by Computer Use.

    Returns True iff cua-driver is installed (or successfully refreshed) when
    the function returns. macOS-only — silently returns False on other
    platforms in ``upgrade`` mode, warns loudly otherwise.
    """
    if platform.system() != "Darwin":
        if upgrade:
            # Silent on non-macOS — callers may invoke this unconditionally.
            return False
        _print_warning("    Computer Use (cua-driver) is macOS-only; skipping.")
        return False

    binary = shutil.which("cua-driver")

    # Not installed → fresh install path (only when caller asked for it).
    if not binary and not upgrade:
        if not shutil.which("curl"):
            _print_warning("    curl not found — install manually:")
            _print_info("      https://github.com/trycua/cua/blob/main/libs/cua-driver/README.md")
            return False
        return _run_cua_driver_installer(label="Installing")

    # Already installed and caller didn't ask to upgrade → just confirm.
    if binary and not upgrade:
        version = cua_driver_version()
        if version:
            _print_success(f"    cua-driver already installed: {version}")
        else:
            _print_success("    cua-driver already installed.")
        _print_info("    Grant macOS permissions if not done yet:")
        _print_info("      System Settings > Privacy & Security > Accessibility")
        _print_info("      System Settings > Privacy & Security > Screen Recording")
        return True

    # upgrade=True path — refresh to the latest upstream release.
    if not shutil.which("curl"):
        _print_warning("    curl not found — cannot refresh cua-driver.")
        return bool(binary)

    before = cua_driver_version() if binary else ""

    ok = _run_cua_driver_installer(label="Refreshing", verbose=False)
    if ok and before:
        after = cua_driver_version()
        if after and after != before:
            _print_success(f"    cua-driver upgraded: {before} → {after}")
        elif after:
            _print_info(f"    cua-driver up to date: {after}")
    return ok


__all__ = [
    "install_cua_driver",
    "cua_driver_version",
    "INSTALL_CMD",
]
