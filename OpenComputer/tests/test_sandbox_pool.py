"""Unit tests for the Docker container reuse pool (M3, sandbox-provider-breadth).

``ContainerPool._docker`` (the subprocess seam) is replaced with an
in-memory stateful fake — no real Docker daemon in CI.
"""

from __future__ import annotations

import asyncio

import pytest

from opencomputer.sandbox.pool import ContainerPool
from plugin_sdk.sandbox import SandboxUnavailable
from tests.sandbox_conformance import docker_probe_ready


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


# --- T3.4: real-Docker integration — container reuse end to end ------------


@pytest.mark.skipif(
    not docker_probe_ready(),
    reason="docker daemon or alpine:latest image unavailable",
)
def test_docker_pool_reuses_one_container_across_calls() -> None:
    """Integration (real Docker): two calls with the same ``container_key``
    share ONE pooled container — proven by a marker written into the
    container's tmpfs in call 1 and read back in call 2 — with exactly
    one ``oc-pool-`` container left running for the key.
    """
    import subprocess
    import uuid as _uuid

    from opencomputer.sandbox.docker import DockerStrategy
    from plugin_sdk.sandbox import SandboxConfig

    key = f"itest-{_uuid.uuid4().hex[:8]}"  # unique per run — no stale collisions
    config = SandboxConfig(container_key=key)
    strat = DockerStrategy()
    pooled = ContainerPool.container_name(strat._pool_key(config))
    try:
        r1 = asyncio.run(
            strat.run(
                ["/bin/sh", "-c", "echo reuse-proof > /tmp/oc-marker"],
                config=config,
            )
        )
        assert r1.exit_code == 0, r1.stderr
        r2 = asyncio.run(
            strat.run(["/bin/sh", "-c", "cat /tmp/oc-marker"], config=config)
        )
        # Same container_key → same pooled container → the marker persists.
        assert r2.exit_code == 0, r2.stderr
        assert "reuse-proof" in r2.stdout
        running = subprocess.run(
            ["docker", "ps", "-q", "--filter", f"name={pooled}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert len(running.stdout.split()) == 1, (
            f"expected exactly one pooled container, got {running.stdout!r}"
        )
    finally:
        subprocess.run(
            ["docker", "rm", "-f", pooled],
            capture_output=True,
            timeout=15,
            check=False,
        )


# --- M4: list / prune / reap ------------------------------------------------


def test_list_pooled_containers_parses_docker_ps() -> None:
    from unittest.mock import MagicMock, patch

    import opencomputer.sandbox.pool as pool_mod

    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = (
        "oc-pool-session-a-111\tUp 3 minutes\t3 minutes ago\n"
        "oc-pool-shared-222\tExited (0)\t1 hour ago\n"
    )
    with (
        patch.object(pool_mod.shutil, "which", return_value="/usr/bin/docker"),
        patch.object(pool_mod.subprocess, "run", return_value=fake),
    ):
        rows = pool_mod.list_pooled_containers()
    assert rows == [
        ("oc-pool-session-a-111", "Up 3 minutes", "3 minutes ago"),
        ("oc-pool-shared-222", "Exited (0)", "1 hour ago"),
    ]


def test_list_pooled_containers_empty_when_docker_absent() -> None:
    from unittest.mock import patch

    import opencomputer.sandbox.pool as pool_mod

    with patch.object(pool_mod.shutil, "which", return_value=None):
        assert pool_mod.list_pooled_containers() == []


def test_prune_pooled_containers_removes_each_listed() -> None:
    from unittest.mock import patch

    import opencomputer.sandbox.pool as pool_mod

    rows = [("oc-pool-a", "Up", "1m"), ("oc-pool-b", "Exited", "2m")]
    rm_calls: list[str] = []

    def fake_rm(name: str) -> bool:
        rm_calls.append(name)
        return True

    with (
        patch.object(pool_mod, "list_pooled_containers", return_value=rows),
        patch.object(pool_mod, "_docker_rm", side_effect=fake_rm),
    ):
        removed = pool_mod.prune_pooled_containers()
    assert removed == ["oc-pool-a", "oc-pool-b"]
    assert rm_calls == ["oc-pool-a", "oc-pool-b"]


def test_reap_removes_each_acquired_pool_container() -> None:
    from unittest.mock import patch

    import opencomputer.sandbox.pool as pool_mod

    pool = ContainerPool()
    pool._lock_for("session-x")  # registers the key, as acquire() does
    pool._lock_for("shared")
    rm_calls: list[str] = []
    with patch.object(
        pool_mod, "_docker_rm", side_effect=lambda n: rm_calls.append(n)
    ):
        pool.reap()
    assert sorted(rm_calls) == ["oc-pool-session-x", "oc-pool-shared"]


def test_oc_sandbox_list_renders_pooled_containers() -> None:
    from unittest.mock import patch

    from typer.testing import CliRunner

    from opencomputer.cli_sandbox import sandbox_app

    fake_rows = [("oc-pool-session-x-abc", "Up 2 minutes", "2 minutes ago")]
    # Patch where the name is USED — cli_sandbox imported it `from … import`.
    with patch(
        "opencomputer.cli_sandbox.list_pooled_containers", return_value=fake_rows
    ):
        result = CliRunner().invoke(sandbox_app, ["list"])
    assert result.exit_code == 0
    assert "oc-pool-session-x-abc" in result.stdout


def test_oc_sandbox_list_empty() -> None:
    from unittest.mock import patch

    from typer.testing import CliRunner

    from opencomputer.cli_sandbox import sandbox_app

    with patch(
        "opencomputer.cli_sandbox.list_pooled_containers", return_value=[]
    ):
        result = CliRunner().invoke(sandbox_app, ["list"])
    assert result.exit_code == 0
    assert "no pooled" in result.stdout.lower()


def test_oc_sandbox_prune_reports_removed() -> None:
    from unittest.mock import patch

    from typer.testing import CliRunner

    from opencomputer.cli_sandbox import sandbox_app

    with patch(
        "opencomputer.cli_sandbox.prune_pooled_containers",
        return_value=["oc-pool-session-x-abc", "oc-pool-shared-def"],
    ):
        result = CliRunner().invoke(sandbox_app, ["prune"])
    assert result.exit_code == 0
    assert "2" in result.stdout
    assert "oc-pool-session-x-abc" in result.stdout
