"""Build hermes-workspace via ``pnpm install`` + ``pnpm build``.

Caching rules:

* If ``dist/server/server.js`` AND ``node_modules/.modules.yaml`` are
  both present AND newer than ``package.json``, we skip everything.
* If ``node_modules/`` exists but lacks ``.modules.yaml``, the previous
  ``pnpm install`` was interrupted — pnpm itself refuses to trust the
  half-baked tree and re-installs from scratch. We treat that case as
  "no cache" and rerun ``pnpm install``.
* If ``dist/`` is missing or stale, we rerun ``pnpm build``.
* ``--force`` skips every cache check.

Subprocess invocation streams stdout/stderr live so the user sees pnpm's
progress (the first install can take 2-5 minutes on a cold cache).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "BuildOutcome",
    "BuildFailed",
    "build_workspace",
    "is_install_complete",
    "is_build_fresh",
]

logger = logging.getLogger("opencomputer.workspace.builder")


class BuildFailed(RuntimeError):  # noqa: N818 — matches LaunchFailed / IsolationFailed naming in OC
    """Raised when ``pnpm install`` or ``pnpm build`` exits non-zero."""

    def __init__(self, step: str, returncode: int) -> None:
        self.step = step
        self.returncode = returncode
        super().__init__(
            f"pnpm {step!r} exited with code {returncode}. "
            "See the pnpm output above for the underlying failure."
        )


@dataclass(frozen=True)
class BuildOutcome:
    """What ``build_workspace`` did."""

    installed: bool
    built: bool
    skipped_reason: str | None
    elapsed_seconds: float

    def summary(self) -> str:
        if not self.installed and not self.built:
            return f"workspace build cache hit ({self.skipped_reason})"
        parts: list[str] = []
        if self.installed:
            parts.append("pnpm install")
        if self.built:
            parts.append("pnpm build")
        return " + ".join(parts) + f" ({self.elapsed_seconds:.1f}s)"


def is_install_complete(workspace_dir: Path) -> bool:
    """Return True iff ``node_modules/`` looks like a complete pnpm install.

    pnpm writes ``node_modules/.modules.yaml`` at the end of a successful
    install. If that file is absent but ``node_modules/`` exists, the
    last install was interrupted (Ctrl+C, OOM, etc.) and a fresh install
    is required.
    """
    nm = workspace_dir / "node_modules"
    if not nm.is_dir():
        return False
    marker = nm / ".modules.yaml"
    return marker.is_file()


def is_build_fresh(workspace_dir: Path) -> bool:
    """Return True iff ``dist/`` is present and newer than ``package.json``.

    Hermes-workspace's vite build writes ``dist/server/server.js`` as
    its final SSR artifact; presence of that file is the most reliable
    "build completed" signal.
    """
    server_js = workspace_dir / "dist" / "server" / "server.js"
    pkg = workspace_dir / "package.json"
    if not server_js.is_file():
        return False
    if not pkg.is_file():
        return False
    try:
        return server_js.stat().st_mtime >= pkg.stat().st_mtime
    except OSError:
        return False


def _run_pnpm(
    workspace_dir: Path,
    step: str,
    *,
    pnpm_path: str,
) -> None:
    """Run a single ``pnpm <step>`` command, streaming output live.

    Raises ``BuildFailed`` on non-zero exit.
    """
    cmd = [pnpm_path, step]
    logger.info(
        "workspace: running %s in %s", " ".join(cmd), workspace_dir,
    )
    # ``stdout=None`` + ``stderr=None`` lets pnpm write directly to the
    # parent terminal's tty so the user sees the progress spinner and
    # download bars. ``stdin=DEVNULL`` prevents pnpm from prompting
    # interactively (it sometimes asks about peer dep mismatches).
    proc = subprocess.run(
        cmd,
        cwd=str(workspace_dir),
        stdin=subprocess.DEVNULL,
        env={**os.environ, "CI": "1"},  # silences pnpm's tty heuristics
        check=False,
    )
    if proc.returncode != 0:
        raise BuildFailed(step=step, returncode=proc.returncode)


def build_workspace(
    workspace_dir: Path,
    *,
    pnpm_path: str | None = None,
    force: bool = False,
) -> BuildOutcome:
    """Run install + build as needed; respect cache unless ``force``.

    Args:
        workspace_dir: hermes-workspace checkout root.
        pnpm_path: absolute path to the ``pnpm`` binary. Pass via the
            ``shutil.which("pnpm")`` lookup done by the caller — we do
            NOT call ``which`` here so the caller has one place to
            handle the "pnpm missing" error.
        force: rebuild even if cache says we don't need to.

    Returns:
        :class:`BuildOutcome` describing what was done.

    Raises:
        BuildFailed: when pnpm itself fails.
        FileNotFoundError: when ``pnpm_path`` is None or doesn't exist.
    """
    if pnpm_path is None or not Path(pnpm_path).is_file():
        raise FileNotFoundError(
            "pnpm binary not found — run `oc workspace doctor` for "
            "install instructions"
        )

    started = time.monotonic()
    install_ok = is_install_complete(workspace_dir)
    build_ok = is_build_fresh(workspace_dir)

    if not force and install_ok and build_ok:
        return BuildOutcome(
            installed=False,
            built=False,
            skipped_reason="node_modules/ and dist/ are up-to-date",
            elapsed_seconds=time.monotonic() - started,
        )

    installed = False
    built = False

    if force or not install_ok:
        if not install_ok and (workspace_dir / "node_modules").exists():
            # Half-baked install — pnpm itself usually recovers but say so.
            print(
                "[workspace] node_modules/ present but no .modules.yaml — "
                "previous install was interrupted; reinstalling",
                file=sys.stderr,
            )
        _run_pnpm(workspace_dir, "install", pnpm_path=pnpm_path)
        installed = True
        # Reinstalled deps may have updated framework files — always
        # rebuild after an install.
        build_ok = False

    if force or not build_ok:
        _run_pnpm(workspace_dir, "build", pnpm_path=pnpm_path)
        built = True

    return BuildOutcome(
        installed=installed,
        built=built,
        skipped_reason=None,
        elapsed_seconds=time.monotonic() - started,
    )
