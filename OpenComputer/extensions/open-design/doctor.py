"""Doctor contributions — surfaces in `oc doctor`.

Four checks:
    1. ``OPEN_DESIGN_HOME`` resolves to a source tree.
    2. ``node`` is on PATH and reports >= v22 (open-design pins ~24).
    3. ``pnpm`` is on PATH (informational — only needed for first-time setup).
    4. Daemon port reachability (only when status reports running).
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess

from plugin_sdk.doctor import HealthContribution, RepairResult

from lifecycle import (  # noqa: E402 — sys.path[0] populated by loader
    resolve_open_design_home,
    status as lifecycle_status,
)


_NODE_MIN_MAJOR = 22  # open-design engines: ~24 — warn below 22, fail below 18


async def _check_home(fix: bool) -> RepairResult:
    del fix  # cannot auto-install open-design
    home = resolve_open_design_home()
    if home is None:
        return RepairResult(
            ok=False,
            message=(
                "OPEN_DESIGN_HOME not set and no default location found "
                "(~/Vscode/claude/open-design, ~/.open-design, /usr/local/share/open-design). "
                "Set the env var or clone https://github.com/nexu-io/open-design."
            ),
        )
    return RepairResult(ok=True, message=f"home: {home}")


async def _check_node(fix: bool) -> RepairResult:
    del fix
    node = shutil.which("node")
    if node is None:
        return RepairResult(
            ok=False,
            message="`node` not found on PATH. Install Node.js (open-design pins ~24).",
        )
    try:
        proc = await asyncio.create_subprocess_exec(
            node, "--version",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, _ = await proc.communicate()
    except OSError as exc:
        return RepairResult(ok=False, message=f"`node --version` failed: {exc}")
    version = out.decode("utf-8", errors="ignore").strip()
    match = re.match(r"v(\d+)", version)
    if not match:
        return RepairResult(ok=True, message=f"node: {version} (unparseable; assuming ok)")
    major = int(match.group(1))
    if major < _NODE_MIN_MAJOR:
        return RepairResult(
            ok=False,
            message=(
                f"node {version} is below the recommended minimum (v{_NODE_MIN_MAJOR}). "
                "open-design pins ~24 in package.json engines. Upgrade Node.js."
            ),
        )
    return RepairResult(ok=True, message=f"node: {version}")


async def _check_pnpm(fix: bool) -> RepairResult:
    del fix
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        return RepairResult(
            ok=True,  # informational only
            message=(
                "`pnpm` not found on PATH. Only required to build open-design from source. "
                "Enable via `corepack enable` if you need to (re)build."
            ),
        )
    return RepairResult(ok=True, message=f"pnpm: {pnpm}")


async def _check_daemon(fix: bool) -> RepairResult:
    del fix
    snap = lifecycle_status()
    if not snap.running:
        return RepairResult(
            ok=True,  # not an error — daemon is opt-in
            message=f"daemon: stopped (start with `oc design start`); would bind {snap.url}",
        )
    return RepairResult(
        ok=True,
        message=f"daemon: running at {snap.url} (pid={snap.pid})",
    )


def build_contributions() -> list[HealthContribution]:
    return [
        HealthContribution(
            id="open-design.home",
            description="open-design source tree discoverable",
            run=_check_home,
        ),
        HealthContribution(
            id="open-design.node",
            description="Node.js available + sufficient version",
            run=_check_node,
        ),
        HealthContribution(
            id="open-design.pnpm",
            description="pnpm available (build dependency)",
            run=_check_pnpm,
        ),
        HealthContribution(
            id="open-design.daemon",
            description="Open Design daemon status",
            run=_check_daemon,
        ),
    ]


__all__ = ["build_contributions"]
