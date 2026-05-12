"""Detect node + pnpm with version checks.

Used by ``oc workspace doctor`` AND as a pre-flight check inside
``oc workspace run`` so a bad environment fails LOUDLY with remediation
hints rather than silently producing cryptic Node errors.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass

__all__ = [
    "MIN_NODE_MAJOR",
    "MIN_PNPM_MAJOR",
    "PrerequisiteStatus",
    "ToolCheck",
    "check_prerequisites",
]


#: Hermes-workspace's package.json declares ``"node": ">=22.0.0"``.
#: We enforce the same floor.
MIN_NODE_MAJOR = 22

#: pnpm 9+ is what hermes-workspace's lockfile expects. Older majors
#: regenerate the lockfile in ways that occasionally break native
#: dependencies; refuse to run.
MIN_PNPM_MAJOR = 9


@dataclass(frozen=True)
class ToolCheck:
    """Outcome of a single CLI-tool probe."""

    name: str
    path: str | None
    version: str | None
    ok: bool
    detail: str

    def status_line(self) -> str:
        if self.ok:
            return f"{self.name}: OK ({self.version}) — {self.path}"
        return f"{self.name}: MISSING — {self.detail}"


@dataclass(frozen=True)
class PrerequisiteStatus:
    """Aggregate status across all required tools."""

    node: ToolCheck
    pnpm: ToolCheck

    @property
    def ok(self) -> bool:
        return self.node.ok and self.pnpm.ok

    def report_lines(self) -> list[str]:
        lines = [self.node.status_line(), self.pnpm.status_line()]
        if not self.ok:
            lines.append("")
            lines.append("Fix:")
            if not self.node.ok:
                lines.append(
                    "  • Install Node.js ≥ 22: https://nodejs.org/ (or via nvm)"
                )
            if not self.pnpm.ok:
                lines.append(
                    "  • Install pnpm ≥ 9: https://pnpm.io/installation "
                    "(`corepack enable && corepack prepare pnpm@latest --activate`)"
                )
        return lines


_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def _parse_major(version_output: str) -> int | None:
    match = _VERSION_RE.search(version_output)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _probe_tool(name: str, *, min_major: int) -> ToolCheck:
    path = shutil.which(name)
    if not path:
        return ToolCheck(
            name=name,
            path=None,
            version=None,
            ok=False,
            detail=f"{name!r} not found on PATH",
        )
    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
    except OSError as exc:
        return ToolCheck(
            name=name,
            path=path,
            version=None,
            ok=False,
            detail=f"failed to invoke {name} --version: {exc}",
        )
    except subprocess.TimeoutExpired:
        return ToolCheck(
            name=name,
            path=path,
            version=None,
            ok=False,
            detail=f"{name} --version timed out after 10s",
        )

    raw = (result.stdout or result.stderr or "").strip()
    major = _parse_major(raw)
    if major is None:
        return ToolCheck(
            name=name,
            path=path,
            version=raw or None,
            ok=False,
            detail=f"could not parse {name} --version output: {raw!r}",
        )
    if major < min_major:
        return ToolCheck(
            name=name,
            path=path,
            version=raw,
            ok=False,
            detail=(
                f"{name} {raw} is below required major {min_major} — "
                f"please upgrade"
            ),
        )
    return ToolCheck(
        name=name,
        path=path,
        version=raw,
        ok=True,
        detail="version OK",
    )


def check_prerequisites() -> PrerequisiteStatus:
    """Run every required CLI-tool probe."""
    return PrerequisiteStatus(
        node=_probe_tool("node", min_major=MIN_NODE_MAJOR),
        pnpm=_probe_tool("pnpm", min_major=MIN_PNPM_MAJOR),
    )
