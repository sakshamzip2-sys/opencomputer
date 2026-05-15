"""Doctor health check for the computer-use plugin.

Probes, in order:

1. Platform — non-macOS → ``skip`` ("macOS only — skipped").
2. ``cua-driver`` binary on PATH → ``warn`` with install hint if missing.
3. The Python ``mcp`` SDK importable → ``warn`` if absent.

When ``oc doctor --fix`` is invoked on macOS with a missing binary, the
contribution shells out to the upstream cua-driver installer and reports
whether the repair succeeded. Accessibility / Screen-Recording permission
grants cannot be automated — the detail string surfaces the manual steps.
"""

from __future__ import annotations

import shutil
import sys

from plugin_sdk.doctor import RepairResult

_DOCTOR_ID = "computer-use"

_PERMS_HINT = (
    "After install, grant permissions: System Settings > Privacy & Security "
    "> Accessibility AND > Screen Recording (both must allow your terminal)."
)


async def run(fix: bool) -> RepairResult:
    """Health contribution entry — see ``plugin_sdk.doctor.HealthRunFn``."""
    # 1) Platform gate — cleanly skip off macOS.
    if sys.platform != "darwin":
        return RepairResult(
            id=_DOCTOR_ID,
            status="skip",
            detail="computer-use is macOS only — skipped on this platform.",
        )

    binary = shutil.which("cua-driver")

    # 2) Missing binary — optionally repair.
    if not binary:
        if fix:
            from cu_installer import install_cua_driver  # type: ignore[import-not-found]

            installed = install_cua_driver(upgrade=False)
            if installed and shutil.which("cua-driver"):
                return RepairResult(
                    id=_DOCTOR_ID,
                    status="pass",
                    detail=f"cua-driver installed. {_PERMS_HINT}",
                    repaired=True,
                )
            return RepairResult(
                id=_DOCTOR_ID,
                status="warn",
                detail=(
                    "cua-driver install did not complete. Re-run manually: "
                    "oc computer-use install"
                ),
            )
        return RepairResult(
            id=_DOCTOR_ID,
            status="warn",
            detail=(
                "cua-driver not installed. Run `oc doctor --fix` or "
                "`oc computer-use install` to install it (macOS background "
                "computer-use binary from github.com/trycua/cua)."
            ),
        )

    # 3) Binary present — verify the mcp SDK is importable (stdio transport).
    try:
        import mcp  # noqa: F401
    except ImportError:
        return RepairResult(
            id=_DOCTOR_ID,
            status="warn",
            detail=(
                "cua-driver is installed but the Python `mcp` SDK is missing. "
                "Install with: pip install mcp"
            ),
        )

    return RepairResult(
        id=_DOCTOR_ID,
        status="pass",
        detail=f"cua-driver installed; mcp SDK present. {_PERMS_HINT}",
    )


__all__ = ["run"]
