"""Tests for fire-and-forget pending-task tracking + drain-on-shutdown.

G.5 / Tier 2.6: F1 audit-log integrity depends on draining in-flight
hook coroutines before the process exits.
"""

from __future__ import annotations

import asyncio

import pytest

from opencomputer.hooks.runner import (
    _pending,
    drain_pending,
    fire_and_forget,
    pending_count,
)


@pytest.fixture(autouse=True)
def clear_pending():
    """Each test starts with no pending tasks."""
    _pending.clear()
    yield
    _pending.clear()


class TestPendingCount:
    @pytest.mark.asyncio
    async def test_empty_initially(self) -> None:
        assert pending_count() == 0

    @pytest.mark.asyncio
    async def test_increments_on_fire(self) -> None:
        ev = asyncio.Event()

        async def slow():
            await ev.wait()

        fire_and_forget(slow())
        assert pending_count() == 1
        ev.set()
        # Yield control so the task can complete + done callback fires
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert pending_count() == 0


class TestDrainPending:
    @pytest.mark.asyncio
    async def test_no_pending_returns_zero_zero(self) -> None:
        completed, cancelled = await drain_pending(timeout=0.1)
        assert (completed, cancelled) == (0, 0)

    @pytest.mark.asyncio
    async def test_drain_completes_quick_tasks(self) -> None:
        results: list[int] = []

        async def quick(n: int):
            await asyncio.sleep(0.01)
            results.append(n)

        for i in range(5):
            fire_and_forget(quick(i))

        completed, cancelled = await drain_pending(timeout=2.0)
        assert completed == 5
        assert cancelled == 0
        assert sorted(results) == [0, 1, 2, 3, 4]

    @pytest.mark.asyncio
    async def test_drain_cancels_stuck_tasks(self) -> None:
        async def stuck():
            await asyncio.sleep(60)  # never finishes within timeout

        fire_and_forget(stuck())
        completed, cancelled = await drain_pending(timeout=0.1)
        assert completed == 0
        assert cancelled == 1
        # And after drain, the stuck task is no longer in _pending (the
        # cancel propagates via _on_done discarding it).
        await asyncio.sleep(0.05)  # allow cancel to settle
        assert pending_count() == 0

    @pytest.mark.asyncio
    async def test_drain_handles_mix_of_quick_and_stuck(self) -> None:
        async def quick():
            await asyncio.sleep(0.01)

        async def stuck():
            await asyncio.sleep(60)

        for _ in range(3):
            fire_and_forget(quick())
        fire_and_forget(stuck())

        completed, cancelled = await drain_pending(timeout=0.5)
        assert completed == 3
        assert cancelled == 1

    @pytest.mark.asyncio
    async def test_drain_swallows_handler_exceptions(self) -> None:
        async def boom():
            raise ValueError("intentional")

        fire_and_forget(boom())
        # Should not raise — exception is logged via _on_done
        completed, cancelled = await drain_pending(timeout=1.0)
        assert completed == 1
        assert cancelled == 0


class TestFireAndForgetTracking:
    @pytest.mark.asyncio
    async def test_done_callback_removes_from_pending(self) -> None:
        async def quick():
            return None

        fire_and_forget(quick())
        assert pending_count() == 1
        # Wait for completion
        for _ in range(5):
            await asyncio.sleep(0.01)
            if pending_count() == 0:
                break
        assert pending_count() == 0

    @pytest.mark.asyncio
    async def test_concurrent_fires_dont_clobber(self) -> None:
        """Many concurrent fires should all register without losing tasks."""
        async def quick():
            await asyncio.sleep(0.01)

        for _ in range(50):
            fire_and_forget(quick())
        assert pending_count() == 50

        # Drain and verify all completed
        completed, cancelled = await drain_pending(timeout=2.0)
        assert completed == 50
        assert cancelled == 0
