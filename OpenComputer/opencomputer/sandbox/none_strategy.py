"""``NoneSandboxStrategy`` — no containment, intentional opt-out.

Useful in tests, single-tenant CI runners, and any context where the
caller already trusts the argv. Every invocation logs a WARNING so the
opt-out is visible in operational logs and audit trails.

Wired in ``SandboxConfig(strategy="none")`` or returned as a fallback
when the user has explicitly disabled sandboxing.
"""

from __future__ import annotations

import asyncio
import logging
import time

from opencomputer.sandbox._common import (
    TIMEOUT_EXIT_CODE,
    TIMEOUT_STDERR,
    decode_stream,
    filtered_env,
)
from plugin_sdk.sandbox import SandboxConfig, SandboxResult, SandboxStrategy

_log = logging.getLogger("opencomputer.sandbox.none")


class NoneSandboxStrategy(SandboxStrategy):
    """Run argv directly with no sandboxing. Logs a WARNING on each call."""

    name = "none"

    def is_available(self) -> bool:
        return True

    def explain(self, argv: list[str], *, config: SandboxConfig) -> list[str]:
        # The "none" strategy is its own wrapped command — no prefix.
        return list(argv)

    async def run(
        self,
        argv: list[str],
        *,
        config: SandboxConfig,
        stdin: bytes | None = None,
        cwd: str | None = None,
    ) -> SandboxResult:
        _log.warning("sandbox: 'none' strategy in use — no containment")
        env = filtered_env(config)
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv,
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
            # Best-effort kill; ignore secondary errors (process may have
            # died between the timeout firing and us reaching this branch).
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
                wrapped_command=list(argv),
                strategy_name=self.name,
            )
        return SandboxResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=decode_stream(stdout),
            stderr=decode_stream(stderr),
            duration_seconds=time.monotonic() - start,
            wrapped_command=list(argv),
            strategy_name=self.name,
        )
