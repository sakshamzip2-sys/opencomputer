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
