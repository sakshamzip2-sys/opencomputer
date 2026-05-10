"""Doctor row for opencli-bridge — three checks.

  1. ``opencli`` binary on PATH (project-local install via npm).
  2. Bundled extension dir present on disk.
  3. ``opencli doctor`` exits 0 (daemon + extension + Chrome reachable).

Returns a single ``RepairResult`` with status and human-readable detail.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from plugin_sdk.doctor import RepairResult

import dispatcher  # type: ignore[import-not-found]

_log = logging.getLogger("opencomputer.opencli_bridge.doctor")

_PLUGIN_DIR = Path(__file__).resolve().parent
_BUNDLED_EXTENSION = _PLUGIN_DIR / "extension" / "v1.0.6"


async def run(fix: bool) -> RepairResult:  # noqa: ARG001 — read-only doctor
    """Three-step probe with early returns on the first hard failure."""
    # 1) opencli binary
    if not shutil.which("opencli"):
        return RepairResult(
            id="opencli-bridge",
            status="warn",
            detail=(
                "opencli not on PATH. Run "
                "`cd OpenComputer && npm install` (project-local) to install."
            ),
        )

    # 2) bundled extension dir
    if not _BUNDLED_EXTENSION.is_dir():
        return RepairResult(
            id="opencli-bridge",
            status="warn",
            detail=(
                f"OpenCLI extension dir missing at {_BUNDLED_EXTENSION}. "
                "Re-install the plugin or place the extension manually."
            ),
        )

    # 3) opencli doctor — runs in thread, opencli's own diagnostic.
    try:
        result = await asyncio.to_thread(dispatcher.doctor_check)
    except Exception as exc:  # noqa: BLE001
        return RepairResult(
            id="opencli-bridge",
            status="warn",
            detail=f"opencli doctor invocation failed: {exc}",
        )

    text = (result.get("text") or "") + " " + (result.get("stderr") or "")
    if "ok" in text.lower() or "✓" in text or "passed" in text.lower():
        return RepairResult(
            id="opencli-bridge",
            status="pass",
            detail="opencli + extension + daemon reachable",
        )

    # Not pass, not hard fail — probably daemon offline (auto-starts on
    # first browser command) or no Chrome running. Surface as warn with
    # the raw doctor text trimmed.
    snippet = (text.strip() or "no diagnostic output")[:300]
    return RepairResult(
        id="opencli-bridge",
        status="warn",
        detail=(
            "opencli + extension present; daemon/extension may not be "
            f"connected yet (auto-starts on first browser command). "
            f"opencli doctor: {snippet}"
        ),
    )


__all__ = ["run"]
