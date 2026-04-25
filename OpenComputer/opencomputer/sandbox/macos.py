"""macOS ``sandbox-exec`` strategy.

Wraps argv in ``sandbox-exec -p <profile> ...``. Builds a per-invocation
TinyScheme profile from :class:`~plugin_sdk.SandboxConfig`:

* deny-by-default
* allow ``process-fork`` + ``process-exec``
* allow ``mach-lookup`` + ``sysctl-read`` + ``ipc-posix-shm`` (required
  to bootstrap any binary on macOS — without these the linker fails
  before ``main`` runs)
* allow ``file-read*`` GLOBALLY (see note below) + ``file-read*`` over
  each ``config.read_paths`` (no-op duplication; documents intent)
* allow ``file-write*`` over a per-invocation tmp dir + each
  ``config.write_paths``
* allow ``network*`` only if ``config.network_allowed=True``

**File-read semantics.** Modern macOS loads the dyld shared cache from
a path that varies by OS version (e.g. ``/Volumes/Preboot/<UUID>/...``
on Sequoia, ``/private/var/db/dyld/...`` historically). Trying to
allowlist every path the loader needs results in a fragile profile
that breaks on every macOS update. Per Chrome / WebKit precedent for
similar sandboxes, we allow ``file-read*`` globally and rely on
write-deny + network-deny + per-invocation tmp dir as the actual
containment boundary. ``config.read_paths`` is currently advisory on
this strategy (the profile would only be tightened if reads were
also denied, which we don't do) — included for parity with the Linux
strategy and forward-compat with a future stricter profile.

**Memory limit:** macOS ``sandbox-exec`` does NOT support a memory cap
(rlimit-style). ``config.memory_mb_limit`` is ignored on this strategy
— callers that need RAM containment must use Docker or wait for a future
``Krunvm`` strategy.

The ``sandbox-exec`` binary is technically deprecated by Apple (per
``man sandbox-exec`` on recent macOS) but remains shipped in the base
system. We use it because it's the only zero-install option on macOS;
a future strategy may target the ``Endpoint Security`` framework instead.
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

_log = logging.getLogger("opencomputer.sandbox.macos")

# Base paths every macOS process needs to read just to start (system
# libraries, system dyld cache, frameworks, resolv.conf for DNS). These
# are read-only and unconditional.
_BASE_READ_PATHS = (
    "/usr/lib",
    "/usr/bin",
    "/bin",
    "/System",
    "/Library/Frameworks",
    "/etc/resolv.conf",
)


def _quote_path(p: str) -> str:
    """Quote a filesystem path for inclusion in a sandbox profile.

    The profile language is TinyScheme — strings are double-quoted with
    backslash escapes. We're conservative: a path containing a literal
    double-quote or backslash is rejected (we have no use case for those
    on macOS, and silent escaping is a footgun).
    """
    if '"' in p or "\\" in p:
        raise ValueError(f"sandbox: refusing path with quote/backslash: {p!r}")
    return f'"{p}"'


def _build_profile(
    config: SandboxConfig,
    *,
    tmp_dir: str,
) -> str:
    """Build a sandbox-exec profile string from ``config``.

    The profile is deny-default with a small allowlist of operations
    that any process needs in order to boot (``mach-lookup``,
    ``sysctl-read``, etc.) and a global ``file-read*`` allow. Writes
    are restricted to ``tmp_dir`` + ``config.write_paths``. Network
    requires explicit opt-in.

    See module docstring for why ``file-read*`` is global rather than
    subpath-restricted.
    """
    write_subpaths = [tmp_dir] + list(config.write_paths)

    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-fork)",
        "(allow process-exec)",
        "(allow signal (target self))",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow ipc-posix-shm)",
        # Global file-read* — see module docstring for rationale.
        "(allow file-read*)",
    ]
    # ``read_paths`` is currently advisory (file-read* is global). The
    # validation here also catches malformed paths early — keep paths
    # that contain quotes/backslashes from sneaking into a future
    # tighter profile.
    for p in (*_BASE_READ_PATHS, *config.read_paths):
        _quote_path(p)  # validates; output unused but error is the point
    if write_subpaths:
        write_clause = " ".join(f"(subpath {_quote_path(p)})" for p in write_subpaths)
        lines.append(f"(allow file-write* {write_clause})")
    if config.network_allowed:
        lines.append("(allow network*)")
    return "\n".join(lines)


class MacOSSandboxExecStrategy(SandboxStrategy):
    """Wraps argv in ``sandbox-exec -p <profile> ...`` on macOS."""

    name = "macos_sandbox_exec"

    def __init__(self) -> None:
        # Cache availability at construction time — capability checks
        # are cheap but we don't want to re-shutil.which on every call.
        self._available = (
            platform.system() == "Darwin" and shutil.which("sandbox-exec") is not None
        )

    def is_available(self) -> bool:
        return self._available

    def _wrap(
        self,
        argv: list[str],
        *,
        config: SandboxConfig,
        tmp_dir: str,
    ) -> list[str]:
        profile = _build_profile(config, tmp_dir=tmp_dir)
        return ["sandbox-exec", "-p", profile, *argv]

    def explain(self, argv: list[str], *, config: SandboxConfig) -> list[str]:
        # ``explain`` should not have side effects, but profile-building
        # needs a tmp dir path. Use a placeholder rather than mkdtemp so
        # repeated explain() calls are referentially transparent.
        return self._wrap(argv, config=config, tmp_dir="/tmp/oc-sandbox-explain")

    async def run(
        self,
        argv: list[str],
        *,
        config: SandboxConfig,
        stdin: bytes | None = None,
        cwd: str | None = None,
    ) -> SandboxResult:
        if config.memory_mb_limit and config.memory_mb_limit > 0:
            _log.debug(
                "sandbox-exec: memory_mb_limit=%d ignored (macOS sandbox-exec has no rlimit)",
                config.memory_mb_limit,
            )
        tmp_dir = tempfile.mkdtemp(prefix="oc-sandbox-")
        wrapped = self._wrap(argv, config=config, tmp_dir=tmp_dir)
        env = filtered_env(config, extras={"TMPDIR": tmp_dir})

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
