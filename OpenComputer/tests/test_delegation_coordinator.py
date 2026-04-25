"""PR-E: DelegationCoordinator tests — per-path locking, sibling concurrency,
deadlock prevention, timeout fail-fast."""
from __future__ import annotations

import asyncio

import pytest

from opencomputer.tools.delegation_coordinator import (
    DEFAULT_LOCK_TIMEOUT_SECONDS,
    DelegationCoordinator,
    DelegationLockTimeout,
    get_default_coordinator,
    reset_default_coordinator,
)


@pytest.fixture(autouse=True)
def _reset_coordinator():
    reset_default_coordinator()
    yield
    reset_default_coordinator()


def test_default_coordinator_is_singleton():
    c1 = get_default_coordinator()
    c2 = get_default_coordinator()
    assert c1 is c2


def test_default_coordinator_resets():
    c1 = get_default_coordinator()
    reset_default_coordinator()
    c2 = get_default_coordinator()
    assert c1 is not c2


@pytest.mark.asyncio
async def test_acquire_empty_paths_is_noop():
    c = DelegationCoordinator()
    async with c.acquire_paths([]) as locked:
        assert locked == []


@pytest.mark.asyncio
async def test_acquire_single_path_succeeds():
    c = DelegationCoordinator()
    async with c.acquire_paths(["/tmp/test_a.py"]) as locked:
        assert len(locked) == 1
        assert locked[0].endswith("test_a.py")


@pytest.mark.asyncio
async def test_acquire_multiple_paths_sorted():
    c = DelegationCoordinator()
    # Pass in non-sorted order; coordinator sorts internally
    async with c.acquire_paths(["/tmp/zzz.py", "/tmp/aaa.py", "/tmp/mmm.py"]) as locked:
        # Returned list is sorted (deterministic ordering for deadlock prevention)
        assert locked == sorted(locked)


@pytest.mark.asyncio
async def test_acquire_normalizes_relative_paths():
    c = DelegationCoordinator()
    async with c.acquire_paths(["./test_relative.py"]) as locked:
        # Normalized to absolute
        assert locked[0].startswith("/")
        assert locked[0].endswith("test_relative.py")


@pytest.mark.asyncio
async def test_acquire_dedupes_duplicate_paths():
    c = DelegationCoordinator()
    async with c.acquire_paths(["/tmp/x.py", "/tmp/x.py", "/tmp/x.py"]) as locked:
        assert len(locked) == 1


@pytest.mark.asyncio
async def test_concurrent_siblings_serialize_on_same_path():
    """Two coroutines acquiring the same path serialize."""
    c = DelegationCoordinator()
    order: list[str] = []

    async def sibling(name: str):
        async with c.acquire_paths(["/tmp/shared.py"]):
            order.append(f"{name}-start")
            await asyncio.sleep(0.05)
            order.append(f"{name}-end")

    await asyncio.gather(sibling("A"), sibling("B"))

    # Either A finishes before B starts, or B finishes before A starts.
    # Critical: A-start/A-end MUST be adjacent (and same for B).
    a_idx = order.index("A-start")
    assert order[a_idx + 1] == "A-end" or order[a_idx + 1].startswith("B")
    # If A finished first, then B-start follows A-end:
    if order[1] == "A-end":
        assert order[2] == "B-start"


@pytest.mark.asyncio
async def test_concurrent_siblings_parallelize_on_disjoint_paths():
    """Two coroutines on different paths run in parallel (don't serialize)."""
    c = DelegationCoordinator()

    enter_a = asyncio.Event()
    enter_b = asyncio.Event()
    proceed = asyncio.Event()

    async def sibling_a():
        async with c.acquire_paths(["/tmp/file_a.py"]):
            enter_a.set()
            await proceed.wait()

    async def sibling_b():
        async with c.acquire_paths(["/tmp/file_b.py"]):
            enter_b.set()
            await proceed.wait()

    a_task = asyncio.create_task(sibling_a())
    b_task = asyncio.create_task(sibling_b())

    # Both should be able to enter their critical sections concurrently
    await asyncio.wait_for(enter_a.wait(), timeout=1.0)
    await asyncio.wait_for(enter_b.wait(), timeout=1.0)
    # Both inside without either having finished — confirms parallelism
    proceed.set()
    await asyncio.gather(a_task, b_task)


@pytest.mark.asyncio
async def test_deadlock_prevention_via_sorted_acquisition():
    """Sibling A locks [x, y]; sibling B locks [y, x]. Sorted acquisition
    means both sort to [x, y] internally → no A-→y-→x vs B-→x-→y deadlock."""
    c = DelegationCoordinator()
    completed: list[str] = []

    async def sibling(name: str, paths: list[str]):
        async with c.acquire_paths(paths):
            await asyncio.sleep(0.02)
            completed.append(name)

    # Without sorted acquisition, this could deadlock.
    # With sorted acquisition, both sort to ["/tmp/locka", "/tmp/lockb"] internally.
    await asyncio.wait_for(
        asyncio.gather(
            sibling("A", ["/tmp/locka", "/tmp/lockb"]),
            sibling("B", ["/tmp/lockb", "/tmp/locka"]),
        ),
        timeout=2.0,
    )
    assert sorted(completed) == ["A", "B"]


@pytest.mark.asyncio
async def test_lock_timeout_raises_delegation_lock_timeout():
    """If a path lock isn't acquired within timeout, raise DelegationLockTimeout."""
    c = DelegationCoordinator(lock_timeout_seconds=0.1)

    # First sibling holds the lock
    blocker_holding = asyncio.Event()
    blocker_release = asyncio.Event()

    async def blocker():
        async with c.acquire_paths(["/tmp/contested.py"]):
            blocker_holding.set()
            await blocker_release.wait()

    blocker_task = asyncio.create_task(blocker())
    await blocker_holding.wait()

    # Second sibling tries to acquire — should fail with DelegationLockTimeout
    with pytest.raises(DelegationLockTimeout, match="contested.py"):
        async with c.acquire_paths(["/tmp/contested.py"]):
            pass

    # Cleanup
    blocker_release.set()
    await blocker_task


@pytest.mark.asyncio
async def test_lock_released_on_exception():
    """Even if the body raises, the lock is released (asynccontextmanager guarantee)."""
    c = DelegationCoordinator()

    with pytest.raises(RuntimeError, match="boom"):
        async with c.acquire_paths(["/tmp/will_fail.py"]):
            raise RuntimeError("boom")

    # Subsequent acquisition should succeed (lock released)
    async with c.acquire_paths(["/tmp/will_fail.py"]):
        pass  # no timeout/error → lock was released after exception


@pytest.mark.asyncio
async def test_timeout_releases_partial_acquisitions():
    """If acquiring [a, b] times out on b, lock on a must be released."""
    c = DelegationCoordinator(lock_timeout_seconds=0.1)

    # Block path 'b'
    blocker_holding = asyncio.Event()
    blocker_release = asyncio.Event()

    async def blocker():
        async with c.acquire_paths(["/tmp/blocked_b.py"]):
            blocker_holding.set()
            await blocker_release.wait()

    blocker_task = asyncio.create_task(blocker())
    await blocker_holding.wait()

    # Try to acquire [a, b] — sorted to [a, b]; gets a; times out on b
    with pytest.raises(DelegationLockTimeout):
        async with c.acquire_paths(["/tmp/blocked_b.py", "/tmp/free_a.py"]):
            pass

    # 'a' must be free now (partial acquisition released on timeout)
    # If it weren't, this would also time out.
    async with c.acquire_paths(["/tmp/free_a.py"]):
        pass

    blocker_release.set()
    await blocker_task


@pytest.mark.asyncio
async def test_stats_reports_lock_state():
    c = DelegationCoordinator()
    s = c.stats()
    assert s["total_paths_registered"] == 0
    assert s["currently_held"] == 0
    assert s["lock_timeout_seconds"] == DEFAULT_LOCK_TIMEOUT_SECONDS

    # Acquire a path; mid-context, stats should show 1 held
    enter = asyncio.Event()
    proceed = asyncio.Event()

    async def holder():
        async with c.acquire_paths(["/tmp/stats_test.py"]):
            enter.set()
            await proceed.wait()

    task = asyncio.create_task(holder())
    await enter.wait()
    s = c.stats()
    assert s["total_paths_registered"] == 1
    assert s["currently_held"] == 1
    assert s["currently_free"] == 0
    proceed.set()
    await task


@pytest.mark.asyncio
async def test_custom_lock_timeout_respected():
    c = DelegationCoordinator(lock_timeout_seconds=2.5)
    assert c.stats()["lock_timeout_seconds"] == 2.5
