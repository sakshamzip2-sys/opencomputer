"""Unit tests for the Docker container reuse pool (M3, sandbox-provider-breadth).

``ContainerPool._docker`` (the subprocess seam) is replaced with an
in-memory stateful fake — no real Docker daemon in CI.
"""

from __future__ import annotations

import asyncio

import pytest

from opencomputer.sandbox.pool import ContainerPool
from plugin_sdk.sandbox import SandboxUnavailable


class _FakeDocker:
    """Stateful in-memory stand-in for ``ContainerPool._docker``.

    Models ``docker inspect`` / ``rm`` / ``run`` against an in-memory view
    of which containers are running vs. stopped-but-present.
    """

    def __init__(self, *, dead: list[str] | None = None, fail_run: bool = False) -> None:
        self.running: set[str] = set()
        self.stopped: set[str] = set(dead or [])  # exist but not running
        self.fail_run = fail_run
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, *args: str) -> tuple[int, str, str]:
        await asyncio.sleep(0)  # yield so concurrent acquires interleave
        self.calls.append(args)
        verb = args[0]
        if verb == "inspect":
            name = args[-1]
            if name in self.running:
                return 0, "true\n", ""
            if name in self.stopped:
                return 0, "false\n", ""
            return 1, "", "Error: No such object"
        if verb == "rm":
            name = args[-1]
            self.running.discard(name)
            self.stopped.discard(name)
            return 0, "", ""
        if verb == "run":
            if self.fail_run:
                return 125, "", "docker: Error response from daemon"
            self.running.add(args[3])  # run, -d, --name, <name>
            return 0, args[3] + "\n", ""
        return 0, "", ""

    def count(self, verb: str) -> int:
        return sum(1 for c in self.calls if c and c[0] == verb)


def _pool_with(fake: _FakeDocker) -> ContainerPool:
    pool = ContainerPool()
    pool._docker = fake  # type: ignore[method-assign]
    return pool


def test_container_name_uses_oc_pool_prefix() -> None:
    assert ContainerPool.container_name("sess-abc123") == "oc-pool-sess-abc123"


def test_acquire_creates_container_on_miss() -> None:
    fake = _FakeDocker()
    pool = _pool_with(fake)
    name = asyncio.run(
        pool.acquire("k1", image="alpine:latest", run_flags=["--network", "none"])
    )
    assert name == "oc-pool-k1"
    assert fake.count("run") == 1
    assert "oc-pool-k1" in fake.running
    # run_flags must be threaded into the `docker run` argv.
    run_call = next(c for c in fake.calls if c[0] == "run")
    assert "--network" in run_call and "none" in run_call
    assert run_call[-3:] == ("tail", "-f", "/dev/null")  # keepalive


def test_acquire_reuses_running_container() -> None:
    fake = _FakeDocker()
    fake.running.add("oc-pool-k1")  # already up
    pool = _pool_with(fake)
    name = asyncio.run(pool.acquire("k1", image="alpine:latest", run_flags=[]))
    assert name == "oc-pool-k1"
    assert fake.count("run") == 0  # reused, not recreated


def test_acquire_recreates_dead_container() -> None:
    """A stopped-but-present container is removed, then recreated (F9)."""
    fake = _FakeDocker(dead=["oc-pool-k1"])
    pool = _pool_with(fake)
    name = asyncio.run(pool.acquire("k1", image="alpine:latest", run_flags=[]))
    assert name == "oc-pool-k1"
    assert fake.count("rm") == 1
    assert fake.count("run") == 1


def test_acquire_raises_sandbox_unavailable_when_create_fails() -> None:
    fake = _FakeDocker(fail_run=True)
    pool = _pool_with(fake)
    with pytest.raises(SandboxUnavailable, match="failed to create"):
        asyncio.run(pool.acquire("k1", image="alpine:latest", run_flags=[]))


def test_per_key_lock_serializes_concurrent_acquire() -> None:
    """Two concurrent acquires of the SAME key create exactly one container.

    Without the per-key lock both tasks would inspect-miss (the fake yields
    at every call) and both would ``docker run`` — a double-create.
    """
    fake = _FakeDocker()
    pool = _pool_with(fake)

    async def _race() -> list[str]:
        return await asyncio.gather(
            pool.acquire("k1", image="alpine:latest", run_flags=[]),
            pool.acquire("k1", image="alpine:latest", run_flags=[]),
        )

    names = asyncio.run(_race())
    assert names == ["oc-pool-k1", "oc-pool-k1"]
    assert fake.count("run") == 1  # the per-key lock prevented a double-create


def test_different_keys_get_distinct_containers() -> None:
    fake = _FakeDocker()
    pool = _pool_with(fake)

    async def _both() -> list[str]:
        return await asyncio.gather(
            pool.acquire("k1", image="alpine:latest", run_flags=[]),
            pool.acquire("k2", image="alpine:latest", run_flags=[]),
        )

    names = asyncio.run(_both())
    assert sorted(names) == ["oc-pool-k1", "oc-pool-k2"]
    assert fake.count("run") == 2


# --- T3.2: docker.py pooled-path integration --------------------------------


def test_docker_pool_key_deterministic_and_config_sensitive() -> None:
    """``_pool_key`` = scope key + a digest of the containment config (P6)."""
    from opencomputer.sandbox.docker import DockerStrategy
    from plugin_sdk.sandbox import SandboxConfig

    strat = DockerStrategy()
    base = SandboxConfig(container_key="session-x")
    assert strat._pool_key(base) == strat._pool_key(base)  # deterministic
    assert strat._pool_key(base).startswith("session-x-")  # scope-key prefix
    # A different image / network keys a DISTINCT pooled container.
    assert strat._pool_key(base) != strat._pool_key(
        SandboxConfig(container_key="session-x", image="python:3.12-slim")
    )
    assert strat._pool_key(base) != strat._pool_key(
        SandboxConfig(container_key="session-x", network_allowed=True)
    )


def test_docker_run_pooled_execs_into_acquired_container() -> None:
    """``run`` with a ``container_key`` acquires a pooled container and
    ``docker exec``s the command into it (mocked — no real Docker)."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from opencomputer.sandbox.docker import DockerStrategy
    from plugin_sdk.sandbox import SandboxConfig, SandboxResult

    fake_pool = MagicMock()
    fake_pool.acquire = AsyncMock(return_value="oc-pool-session-x-abc123def456")

    captured: dict[str, tuple[str, ...]] = {}

    async def fake_exec(*argv: str, **_kw: object):
        del _kw  # absorb stdin/stdout/stderr/cwd kwargs — unused here
        captured["argv"] = argv
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"pooled-ok\n", b""))
        proc.returncode = 0
        return proc

    strat = DockerStrategy()
    with (
        patch("opencomputer.sandbox.docker._get_pool", return_value=fake_pool),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
    ):
        result = asyncio.run(
            strat.run(
                ["echo", "hi"],
                config=SandboxConfig(container_key="session-x"),
            )
        )
    assert isinstance(result, SandboxResult)
    assert result.exit_code == 0
    assert result.stdout == "pooled-ok\n"
    assert result.strategy_name == "docker"
    fake_pool.acquire.assert_awaited_once()
    argv = captured["argv"]
    assert argv[:2] == ("docker", "exec")
    assert "oc-pool-session-x-abc123def456" in argv
    assert argv[-2:] == ("echo", "hi")


# --- T3.3: BashTool threads the resolved container key ---------------------


def test_bash_threads_resolved_container_key_into_sandbox_config() -> None:
    """``BashTool._execute_in_sandbox`` reads ``runtime.custom``'s
    ``sandbox_container_key`` and threads it onto the ``SandboxConfig``
    the backend receives — the live loop -> runtime -> bash path."""
    from opencomputer.tools.bash import BashTool
    from plugin_sdk.core import ToolCall
    from plugin_sdk.runtime_context import RuntimeContext
    from plugin_sdk.sandbox import SandboxResult

    captured: dict[str, object] = {}

    class _RecordingStrategy:
        name = "recording"

        async def run(self, argv, *, config, stdin=None, cwd=None):
            del stdin, cwd  # ABC signature; unused by this recording double
            captured["container_key"] = config.container_key
            return SandboxResult(
                exit_code=0,
                stdout="",
                stderr="",
                duration_seconds=0.0,
                wrapped_command=list(argv),
                strategy_name=self.name,
            )

    runtime = RuntimeContext()
    runtime.custom["sandbox_container_key"] = "session-abc"
    BashTool.set_runtime(runtime)
    try:
        result = asyncio.run(
            BashTool()._execute_in_sandbox(
                call=ToolCall(id="t1", name="Bash", arguments={}),
                cmd="echo hi",
                timeout=60,
                strategy=_RecordingStrategy(),
                warn_prefix="",
            )
        )
    finally:
        BashTool.set_runtime(RuntimeContext())  # reset the class-level runtime
    assert result is not None and result.is_error is False
    assert captured["container_key"] == "session-abc"
