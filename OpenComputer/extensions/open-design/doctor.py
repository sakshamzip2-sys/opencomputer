"""Doctor contributions — surfaces in `oc doctor`.

Five checks (run order matches the order shown in `oc doctor`):

1. ``open-design.home`` — source tree resolvable via ``OPEN_DESIGN_HOME``
   env or one of the default candidate locations.
2. ``open-design.node`` — ``node`` binary on PATH and version >= 22.
3. ``open-design.pnpm`` — ``pnpm`` on PATH (informational; only needed
   for first-time build).
4. ``open-design.web-built`` — Next.js export exists at
   ``apps/web/out/index.html`` (otherwise daemon GET / returns 404).
5. ``open-design.daemon`` — current daemon process status (running /
   stopped) plus whether the daemon is actually serving the SPA.

Each check returns a :class:`plugin_sdk.doctor.RepairResult` with the
typed ``status`` literal — ``"pass"`` / ``"warn"`` / ``"fail"`` /
``"skip"``. ``oc doctor`` aggregates these into the health summary.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess

from lifecycle import (  # noqa: E402 — sys.path[0] populated by loader
    WEB_INDEX_REL_PATH,
    resolve_open_design_home,
)
from lifecycle import (
    status as lifecycle_status,
)

from plugin_sdk.doctor import HealthContribution, RepairResult

#: Minimum Node major version we accept. open-design pins ~24 in its
#: package.json engines field, but the daemon binary runs fine on Node
#: 22+ in practice (we've verified it locally). Below 22, the runtime
#: misses ESM features the daemon relies on.
_NODE_MIN_MAJOR = 22


async def _check_home(fix: bool) -> RepairResult:
    """Status semantics:
    - ``pass``: home resolved.
    - ``skip``: open-design not installed (plugin is auto-enabled but
      open-design itself is optional — not a failure).
    """
    del fix  # cannot auto-install open-design from here
    home = resolve_open_design_home()
    if home is None:
        return RepairResult(
            id="open-design.home",
            status="skip",
            detail=(
                "open-design not installed. Optional. To use the Design "
                "tab, clone https://github.com/nexu-io/open-design to "
                "~/.open-design or set OPEN_DESIGN_HOME."
            ),
        )
    return RepairResult(
        id="open-design.home",
        status="pass",
        detail=f"home: {home}",
    )


async def _check_node(fix: bool) -> RepairResult:
    """``skip`` when open-design isn't installed (no point checking
    Node), ``pass`` when version >= MIN_MAJOR, ``warn`` for unparseable
    or sub-min versions (the plugin still loads; only daemon start
    breaks at runtime with an actionable error)."""
    del fix
    if resolve_open_design_home() is None:
        return RepairResult(
            id="open-design.node",
            status="skip",
            detail="skipped — open-design not installed",
        )
    node = shutil.which("node")
    if node is None:
        return RepairResult(
            id="open-design.node",
            status="warn",
            detail="`node` not found on PATH; install Node.js 22+ before `oc design start`.",
        )
    try:
        proc = await asyncio.create_subprocess_exec(
            node, "--version",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, _ = await proc.communicate()
    except OSError as exc:
        return RepairResult(
            id="open-design.node",
            status="warn",
            detail=f"`node --version` failed: {exc}",
        )
    version = out.decode("utf-8", errors="ignore").strip()
    match = re.match(r"v(\d+)", version)
    if not match:
        return RepairResult(
            id="open-design.node",
            status="warn",
            detail=f"node version unparseable: {version!r}",
        )
    major = int(match.group(1))
    if major < _NODE_MIN_MAJOR:
        return RepairResult(
            id="open-design.node",
            status="warn",
            detail=(
                f"node {version} is below the recommended minimum "
                f"(v{_NODE_MIN_MAJOR}). open-design pins ~24. "
                "`oc design start` will fail until upgraded."
            ),
        )
    return RepairResult(
        id="open-design.node",
        status="pass",
        detail=f"node: {version}",
    )


async def _check_pnpm(fix: bool) -> RepairResult:
    del fix
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        return RepairResult(
            id="open-design.pnpm",
            status="warn",
            detail=(
                "`pnpm` not found on PATH. Only required to build open-design "
                "from source. Run `corepack enable` if you need to (re)build."
            ),
        )
    return RepairResult(
        id="open-design.pnpm",
        status="pass",
        detail=f"pnpm: {pnpm}",
    )


async def _check_web_built(fix: bool) -> RepairResult:
    """The Next.js SPA must be exported to ``apps/web/out`` for the
    daemon's ``express.static`` middleware to serve ``GET /``.

    Without this, ``http://127.0.0.1:7456/`` returns "Cannot GET /" and
    the Hermes Design tab iframes a 404. Flagged as a hard failure with
    an actionable build command.
    """
    del fix
    home = resolve_open_design_home()
    if home is None:
        return RepairResult(
            id="open-design.web-built",
            status="skip",
            detail="skipped — open-design not installed",
        )
    index = home / WEB_INDEX_REL_PATH
    if not index.is_file():
        # Home is present but SPA isn't built — actionable broken state
        # (user installed open-design but missed the web build). Warn
        # rather than fail so we don't shadow more critical doctor rows.
        return RepairResult(
            id="open-design.web-built",
            status="warn",
            detail=(
                f"SPA not built: {index} missing. "
                f"Run `pnpm --filter @open-design/web build` in {home}, "
                "then `oc design restart`."
            ),
        )
    return RepairResult(
        id="open-design.web-built",
        status="pass",
        detail=f"SPA: built at {home / 'apps/web/out'}",
    )


async def _check_daemon(fix: bool) -> RepairResult:
    """Aggregate daemon-process + SPA-served status.

    ``status="pass"`` only when the daemon is up *and* serving the SPA.
    Daemon-down is ``status="skip"`` (it is opt-in, not an error).
    Daemon-up-but-SPA-missing is ``status="warn"`` — the listener is
    healthy but the iframe target will 404.
    """
    del fix
    snap = lifecycle_status()
    if not snap.running:
        return RepairResult(
            id="open-design.daemon",
            status="skip",
            detail=f"daemon: stopped (start with `oc design start`); would bind {snap.url}",
        )
    if not snap.web_served:
        return RepairResult(
            id="open-design.daemon",
            status="warn",
            detail=(
                f"daemon: running at {snap.url} (pid={snap.pid}) but SPA missing "
                f"— {snap.error or 'unknown reason'}"
            ),
        )
    return RepairResult(
        id="open-design.daemon",
        status="pass",
        detail=f"daemon: running at {snap.url} (pid={snap.pid}); SPA served",
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
            id="open-design.web-built",
            description="Open Design web SPA built (apps/web/out)",
            run=_check_web_built,
        ),
        HealthContribution(
            id="open-design.daemon",
            description="Open Design daemon status",
            run=_check_daemon,
        ),
    ]


__all__ = ["build_contributions"]
