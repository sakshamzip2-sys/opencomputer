"""``SSHSandboxStrategy`` — execute argv on a remote host over SSH.

This is **not** a security sandbox in the macOS / bwrap / Docker sense. It
relies on the remote host being a trusted execution environment (the user
explicitly opted in via ``SandboxConfig(strategy="ssh", ssh_host=...)``).
The value is *isolation by separation* — runs touch the remote filesystem,
not the local one — and *consistent execution venue* (SSH-into-fly-machine
or SSH-into-VPS for power users).

Phase 1.2 of the catch-up plan (real-gui-velvet-lemur).

Mitigations
-----------

* ``ssh_host`` is regex-validated before each use. Shell metacharacters
  (``;``, ``$``, backticks, spaces) are refused outright — no command
  injection via host string.
* Remote argv is built with :func:`shlex.join`, never via string format,
  so the wrapped command is a single argument to ``sh -c`` on the far end.
* ``-o BatchMode=yes`` so the strategy never blocks asking for a password
  (CI-safe, fail-fast on missing keys).
* ``-o ConnectTimeout=10`` so a dead host fails in ≤10 s instead of the
  default 2-minute TCP timeout.
* Wall-clock cap from ``config.cpu_seconds_limit`` enforced via
  :func:`asyncio.wait_for`, mirroring the other strategies.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import shutil
import time

from opencomputer.sandbox._common import (
    TIMEOUT_EXIT_CODE,
    TIMEOUT_STDERR,
    decode_stream,
    filtered_env,
)
from plugin_sdk.sandbox import (
    SandboxConfig,
    SandboxResult,
    SandboxStrategy,
    SandboxUnavailable,
)

# host: optional user@, then a label of [A-Za-z0-9_.-]+. No spaces / shell
# metacharacters. Anchored. IPv4 dotted-quads naturally pass.
_VALID_HOST = re.compile(r"^([A-Za-z0-9_-]+@)?[A-Za-z0-9_.-]+$")


def _validate_host(host: str | None) -> str:
    if not host:
        raise SandboxUnavailable("ssh strategy requires SandboxConfig(ssh_host=...)")
    if not _VALID_HOST.fullmatch(host):
        raise SandboxUnavailable(f"ssh strategy refuses unsafe host string: {host!r}")
    return host


class SSHSandboxStrategy(SandboxStrategy):
    """Run argv on a remote host via ``ssh user@host``.

    Trust model: the *remote host* is trusted; SSH only adds isolation by
    separation, not in-host containment. Pair with a remote sandbox
    (e.g. ssh into a docker host) for defense in depth.
    """

    name = "ssh"

    def is_available(self) -> bool:
        # Available iff the ``ssh`` binary is on PATH. We do NOT check
        # ssh_host here because is_available() is called without a config
        # in the auto_strategy flow; host validation happens at run time.
        return shutil.which("ssh") is not None

    def explain(self, argv: list[str], *, config: SandboxConfig) -> list[str]:
        host = _validate_host(config.ssh_host)
        remote_cmd = shlex.join(argv)
        return self._build_argv(host, remote_cmd)

    @staticmethod
    def _build_argv(host: str, remote_cmd: str) -> list[str]:
        return [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=30",
            host,
            remote_cmd,
        ]

    async def run(
        self,
        argv: list[str],
        *,
        config: SandboxConfig,
        stdin: bytes | None = None,
        cwd: str | None = None,
    ) -> SandboxResult:
        if not self.is_available():
            raise SandboxUnavailable("ssh strategy: 'ssh' binary not found on PATH")
        host = _validate_host(config.ssh_host)
        remote_cmd = shlex.join(argv)
        if cwd:
            remote_cmd = f"cd {shlex.quote(cwd)} && {remote_cmd}"
        ssh_argv = self._build_argv(host, remote_cmd)

        env = filtered_env(config)
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *ssh_argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
                wrapped_command=ssh_argv,
                strategy_name=self.name,
            )
        return SandboxResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=decode_stream(stdout),
            stderr=decode_stream(stderr),
            duration_seconds=time.monotonic() - start,
            wrapped_command=ssh_argv,
            strategy_name=self.name,
        )
