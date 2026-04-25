"""Linux ``bwrap`` (bubblewrap) strategy.

Wraps argv in a minimal ``bwrap`` invocation that:

* read-only-binds the system roots needed for typical interpreters
  (``/usr``, ``/lib``, ``/lib64``, ``/etc/resolv.conf``)
* mounts ``/proc`` + ``/dev`` (so binaries can introspect themselves)
* unshares PID + IPC + UTS namespaces
* binds an ephemeral tmp dir at ``/tmp``
* ``--unshare-net`` when ``config.network_allowed=False``
* binds each ``config.read_paths`` read-only and each
  ``config.write_paths`` read-write
* enforces ``config.memory_mb_limit`` via ``prlimit`` when available
  (best-effort — debug-logged + skipped if ``prlimit`` is missing)

Wall-clock cap enforced via :func:`asyncio.wait_for`; on overrun we
SIGKILL the bwrap process (which kills the entire unshared PID
namespace because we set ``--die-with-parent``).
"""

from __future__ import annotations

import asyncio
import logging
import platform
import shutil
import tempfile
import time

from opencomputer.sandbox._common import (
    TIMEOUT_EXIT_CODE,
    TIMEOUT_STDERR,
    decode_stream,
    filtered_env,
)
from plugin_sdk.sandbox import SandboxConfig, SandboxResult, SandboxStrategy

_log = logging.getLogger("opencomputer.sandbox.linux")

# Read-only system paths every Linux process needs. We bind these
# unconditionally regardless of ``config.read_paths``. ``/lib64`` is
# x86_64 only — it's tolerated when missing because bwrap will simply
# refuse the bind (we strip it before passing if the host doesn't have
# it; see ``_resolve_base_binds``).
_BASE_RO_BINDS = (
    "/usr",
    "/lib",
    "/lib64",
    "/etc/resolv.conf",
)


def _resolve_base_binds() -> list[tuple[str, str]]:
    """Return the subset of ``_BASE_RO_BINDS`` that actually exists on host.

    Skipping non-existent paths is necessary because bwrap fails the
    whole invocation if ANY ``--ro-bind`` source is missing.
    """
    import os.path as _p

    return [(s, s) for s in _BASE_RO_BINDS if _p.exists(s)]


class LinuxBwrapStrategy(SandboxStrategy):
    """Wraps argv in ``bwrap ...`` on Linux."""

    name = "linux_bwrap"

    def __init__(self) -> None:
        self._available = (
            platform.system() == "Linux" and shutil.which("bwrap") is not None
        )
        # ``prlimit`` is part of util-linux; absent on minimal images.
        # Cache so we don't shutil.which on every call.
        self._has_prlimit = shutil.which("prlimit") is not None

    def is_available(self) -> bool:
        return self._available

    def _wrap(
        self,
        argv: list[str],
        *,
        config: SandboxConfig,
        tmp_dir: str,
    ) -> list[str]:
        cmd: list[str] = []
        # Memory cap via prlimit, if available. ``prlimit --as=N -- bwrap ...``
        # caps the address space of the entire process tree. On Linux this is
        # the cleanest "best-effort" RAM cap that doesn't require cgroups.
        if (
            self._has_prlimit
            and config.memory_mb_limit
            and config.memory_mb_limit > 0
        ):
            cap_bytes = config.memory_mb_limit * 1024 * 1024
            cmd.extend(["prlimit", f"--as={cap_bytes}", "--"])
        elif config.memory_mb_limit and config.memory_mb_limit > 0:
            _log.debug(
                "bwrap: memory_mb_limit=%d ignored (prlimit not available)",
                config.memory_mb_limit,
            )

        cmd.extend(
            [
                "bwrap",
                "--die-with-parent",
                "--unshare-pid",
                "--unshare-ipc",
                "--unshare-uts",
                "--proc", "/proc",
                "--dev", "/dev",
            ]
        )
        for src, dst in _resolve_base_binds():
            cmd.extend(["--ro-bind", src, dst])
        for p in config.read_paths:
            cmd.extend(["--ro-bind", p, p])
        for p in config.write_paths:
            cmd.extend(["--bind", p, p])
        cmd.extend(["--bind", tmp_dir, "/tmp"])
        if not config.network_allowed:
            cmd.append("--unshare-net")
        cmd.append("--")
        cmd.extend(argv)
        return cmd

    def explain(self, argv: list[str], *, config: SandboxConfig) -> list[str]:
        # See note in MacOSSandboxExecStrategy.explain — placeholder tmp dir
        # so explain() has no side effects.
        return self._wrap(argv, config=config, tmp_dir="/tmp/oc-sandbox-explain")

    async def run(
        self,
        argv: list[str],
        *,
        config: SandboxConfig,
        stdin: bytes | None = None,
        cwd: str | None = None,
    ) -> SandboxResult:
        tmp_dir = tempfile.mkdtemp(prefix="oc-sandbox-")
        wrapped = self._wrap(argv, config=config, tmp_dir=tmp_dir)
        env = filtered_env(config, extras={"TMPDIR": "/tmp"})

        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *wrapped,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=stdin),
                timeout=config.cpu_seconds_limit,
            )
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
            return SandboxResult(
                exit_code=TIMEOUT_EXIT_CODE,
                stdout="",
                stderr=TIMEOUT_STDERR,
                duration_seconds=time.monotonic() - start,
                wrapped_command=wrapped,
                strategy_name=self.name,
            )
        return SandboxResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=decode_stream(stdout),
            stderr=decode_stream(stderr),
            duration_seconds=time.monotonic() - start,
            wrapped_command=wrapped,
            strategy_name=self.name,
        )
