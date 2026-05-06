"""Tests for QueueManager — followup (default) + interrupt modes."""

from __future__ import annotations

import asyncio

import pytest

from opencomputer.gateway.queue_manager import (
    QueueManager,
    get_active_manager,
    set_active_manager,
)
from plugin_sdk.queue import (
    ALL_QUEUE_MODES,
    DEFAULT_QUEUE_MODE,
    QueueConfig,
)


def test_default_mode_is_followup():
    assert DEFAULT_QUEUE_MODE == "followup"
    qm = QueueManager()
    assert qm.default_mode == "followup"


def test_all_queue_modes_tuple():
    assert "followup" in ALL_QUEUE_MODES
    assert "interrupt" in ALL_QUEUE_MODES
    assert len(ALL_QUEUE_MODES) == 2  # promote when adding more


def test_queue_config_dataclass_default():
    cfg = QueueConfig()
    assert cfg.mode == "followup"


def test_queue_config_frozen():
    cfg = QueueConfig(mode="interrupt")
    with pytest.raises(Exception):
        cfg.mode = "followup"  # type: ignore[misc]


def test_invalid_default_mode_raises():
    with pytest.raises(ValueError, match="unknown queue mode"):
        QueueManager(default_mode="bogus")  # type: ignore[arg-type]


def test_set_default_mode_validates():
    qm = QueueManager()
    qm.set_default_mode("interrupt")
    assert qm.default_mode == "interrupt"
    with pytest.raises(ValueError):
        qm.set_default_mode("bogus")  # type: ignore[arg-type]


def test_per_session_override():
    qm = QueueManager()
    assert qm.get_session_mode("s1") == "followup"
    qm.set_session_mode("s1", "interrupt")
    assert qm.get_session_mode("s1") == "interrupt"
    # Other sessions unaffected.
    assert qm.get_session_mode("s2") == "followup"
    qm.clear_session_mode("s1")
    assert qm.get_session_mode("s1") == "followup"


def test_set_session_mode_validates():
    qm = QueueManager()
    with pytest.raises(ValueError):
        qm.set_session_mode("s", "bogus")  # type: ignore[arg-type]


async def test_followup_serializes_two_concurrent_runs():
    """Default mode: two acquires for the same key serialize."""
    qm = QueueManager()
    order: list[str] = []

    async def run(label: str):
        async with qm.acquire("p", "s"):
            order.append(f"{label}-start")
            await asyncio.sleep(0.05)
            order.append(f"{label}-end")

    # Start B slightly after A; B should wait for A to finish.
    a = asyncio.create_task(run("a"))
    await asyncio.sleep(0.01)
    b = asyncio.create_task(run("b"))
    await asyncio.gather(a, b)

    assert order == ["a-start", "a-end", "b-start", "b-end"]


async def test_interrupt_cancels_in_flight():
    """Interrupt mode: second acquire cancels the first."""
    qm = QueueManager()
    qm.set_session_mode("s", "interrupt")
    cancelled: list[bool] = []

    async def long_run():
        try:
            async with qm.acquire("p", "s"):
                await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    a = asyncio.create_task(long_run())
    await asyncio.sleep(0.05)  # let `a` enter the body

    # Second acquire on same (profile,session) cancels `a`.
    async with qm.acquire("p", "s"):
        # Inside the second acquire — the first task should have been cancelled.
        pass

    # Give the cancelled task a moment to record.
    with pytest.raises(asyncio.CancelledError):
        await a

    assert cancelled == [True]


async def test_different_sessions_dont_block_each_other():
    qm = QueueManager()
    order: list[str] = []

    async def run_s1():
        async with qm.acquire("p", "s1"):
            order.append("s1-start")
            await asyncio.sleep(0.05)
            order.append("s1-end")

    async def run_s2():
        async with qm.acquire("p", "s2"):
            order.append("s2-start")
            await asyncio.sleep(0.01)
            order.append("s2-end")

    await asyncio.gather(run_s1(), run_s2())
    # Both starts come before either end → ran in parallel.
    assert order.index("s1-start") < order.index("s1-end")
    assert order.index("s2-start") < order.index("s2-end")
    # s2 was faster so it finishes first.
    assert "s2-end" in order


def test_set_active_manager_roundtrip():
    qm = QueueManager()
    set_active_manager(qm)
    try:
        assert get_active_manager() is qm
    finally:
        set_active_manager(None)
    assert get_active_manager() is None
