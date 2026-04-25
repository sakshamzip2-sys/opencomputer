"""Docker strategy — cross-platform containment via ``docker run --rm ...``.

Available on any host with the ``docker`` CLI on PATH AND a reachable
daemon (verified once at construction via ``docker info``). Each call
spawns a transient container; we set ``--rm`` so cleanup is automatic
and ``--name`` so we can ``docker kill`` if the wrapper process gets
orphaned during a timeout.

Memory cap: ``--memory <N>m``. Network: ``--network none`` when
``config.network_allowed=False``. Path bindings: ``-v src:dst:ro`` for
``read_paths``, ``-v src:dst:rw`` for ``write_paths``.

Image: ``config.image`` (default ``alpine:latest``). Caller is
responsible for ensuring the image is pulled — we don't auto-pull.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import time
import uuid

from opencomputer.sandbox._common import (
    TIMEOUT_EXIT_CODE,
    TIMEOUT_STDERR,
    decode_stream,
    filtered_env,
)
from plugin_sdk.sandbox import SandboxConfig, SandboxResult, SandboxStrategy

_log = logging.getLogger("opencomputer.sandbox.docker")


def _derive_cpu_quota(cpu_seconds_limit: int) -> int:
    """Map wall-clock budget → CPU count for ``docker run --cpus``.

    A 60 s budget gets 2 cores; small budgets clamp to 1; never above 2.
    The shape is ``min(2, max(1, cpu_seconds_limit // 30))`` so a 30 s
    budget gets 1 core, 60 s gets 2 cores, longer doesn't accumulate
    further (host courtesy — sandboxed code shouldn't monopolise).
    """
    return min(2, max(1, cpu_seconds_limit // 30))


class DockerStrategy(SandboxStrategy):
    """Wraps argv in ``docker run --rm ...``."""

    name = "docker"

    def __init__(self) -> None:
        self._available = self._probe_docker()

    @staticmethod
    def _probe_docker() -> bool:
        """Check ``docker`` is on PATH AND ``docker info`` returns 0.

        Synchronous (called once from ``__init__``). Best-effort: the
        timeout guards against a hung daemon and any other exception
        triggers ``False`` so we never propagate odd shell errors.
        """
        if shutil.which("docker") is None:
            return False
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            return result.returncode == 0
        except (subprocess.SubprocessError, OSError):
            return False

    def is_available(self) -> bool:
        return self._available

    def _wrap(
        self,
        argv: list[str],
        *,
        config: SandboxConfig,
        container_name: str,
    ) -> list[str]:
        cmd: list[str] = [
            "docker", "run",
            "--rm",
            "-i",
            "--name", container_name,
            "--memory", f"{config.memory_mb_limit}m",
            "--cpus", str(_derive_cpu_quota(config.cpu_seconds_limit)),
        ]
        if not config.network_allowed:
            cmd.extend(["--network", "none"])
        for p in config.read_paths:
            cmd.extend(["-v", f"{p}:{p}:ro"])
        for p in config.write_paths:
            cmd.extend(["-v", f"{p}:{p}:rw"])
        # Pass through allowed env vars. We use ``-e KEY=VALUE`` rather
        # than ``--env-file`` so the env stays purely in argv (easier to
        # audit + no temp file to clean up).
        env_pass = filtered_env(config)
        for k, v in env_pass.items():
            cmd.extend(["-e", f"{k}={v}"])
        cmd.append(config.image)
        cmd.extend(argv)
        return cmd

    def explain(self, argv: list[str], *, config: SandboxConfig) -> list[str]:
        return self._wrap(argv, config=config, container_name="oc-sandbox-explain")

    async def run(
        self,
        argv: list[str],
        *,
        config: SandboxConfig,
        stdin: bytes | None = None,
        cwd: str | None = None,
    ) -> SandboxResult:
        container_name = f"oc-sandbox-{uuid.uuid4().hex[:12]}"
        wrapped = self._wrap(argv, config=config, container_name=container_name)
        # Docker daemon already isolates env from the host shell, so we
        # don't need to pass ``env=`` here (the ``-e`` flags inside
        # ``wrapped`` handle that). cwd similarly applies to the docker
        # CLI process, not the container.
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *wrapped,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=stdin),
                timeout=config.cpu_seconds_limit,
            )
        except TimeoutError:
            # Two-step kill: SIGKILL the docker CLI, then ``docker kill``
            # the container in case the CLI exited but the container is
            # still running (orphaned). This is the cross-platform
            # equivalent of bwrap's ``--die-with-parent``.
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
            try:
                kill_proc = await asyncio.create_subprocess_exec(
                    "docker", "kill", container_name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(kill_proc.wait(), timeout=5)
            except Exception as e:  # noqa: BLE001
                _log.debug("docker kill of orphan %s failed: %s", container_name, e)
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
