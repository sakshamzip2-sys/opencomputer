"""Tests for the gateway-embedded kanban dispatcher loop (Wave 6.E.1).

Covers:
- ``read_kanban_dispatch_config`` parses a config.yaml block correctly
- Defaults applied when block missing / wrong type
- Loop exits cleanly when ``stop()`` is called
- Loop calls ``dispatch_once`` on each tick (verified via monkeypatch)
- Tick errors back off without crashing
- Loop is NOT started when ``dispatch_in_gateway: false``
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from opencomputer.gateway.kanban_dispatcher import (
    DEFAULT_DISPATCH_IN_GATEWAY,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_MAX_SPAWN,
    KanbanDispatcherLoop,
    read_kanban_dispatch_config,
)

# ---- Config parsing ----


def test_read_config_defaults_for_empty():
    enabled, interval, max_spawn = read_kanban_dispatch_config({})
    assert enabled == DEFAULT_DISPATCH_IN_GATEWAY
    assert interval == DEFAULT_INTERVAL_SECONDS
    assert max_spawn == DEFAULT_MAX_SPAWN


def test_read_config_defaults_for_none():
    enabled, _, _ = read_kanban_dispatch_config(None)
    assert enabled is True


def test_read_config_disabled():
    enabled, _, _ = read_kanban_dispatch_config(
        {"kanban": {"dispatch_in_gateway": False}}
    )
    assert enabled is False


def test_read_config_custom_values():
    enabled, interval, max_spawn = read_kanban_dispatch_config(
        {"kanban": {
            "dispatch_in_gateway": True,
            "dispatch_interval_seconds": 10,
            "max_spawn": 8,
        }}
    )
    assert enabled is True
    assert interval == 10.0
    assert max_spawn == 8


def test_read_config_rejects_wrong_types():
    """Malformed values fall through to defaults (fail-open)."""
    enabled, interval, max_spawn = read_kanban_dispatch_config(
        {"kanban": {
            "dispatch_in_gateway": "yes",       # not bool
            "dispatch_interval_seconds": -1,    # not positive
            "max_spawn": "high",                # not int
        }}
    )
    assert enabled == DEFAULT_DISPATCH_IN_GATEWAY
    assert interval == DEFAULT_INTERVAL_SECONDS
    assert max_spawn == DEFAULT_MAX_SPAWN


# ---- Loop lifecycle ----


@pytest.mark.asyncio
async def test_loop_calls_dispatch_once_per_tick(tmp_path: Path, monkeypatch):
    """The loop should invoke ``dispatch_once`` at least once before stop."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    calls = []

    class _FakeResult:
        spawned = []
        crashed = []
        timed_out = []
        auto_blocked = []
        promoted = 0
        reclaimed = 0

    def fake_dispatch_once(conn, **kwargs):
        calls.append(kwargs)
        return _FakeResult()

    class _FakeConnCM:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_connect():
        return _FakeConnCM()

    # Tight tick interval so the test isn't slow.
    loop = KanbanDispatcherLoop(interval_seconds=0.05, max_spawn=2)
    with patch("opencomputer.kanban.db.dispatch_once", fake_dispatch_once), \
         patch("opencomputer.kanban.db.connect", fake_connect):
        task = asyncio.create_task(loop.run_forever())
        await asyncio.sleep(0.18)
        await loop.stop()
        await asyncio.wait_for(task, timeout=2.0)

    assert len(calls) >= 2
    # Each tick passes max_spawn through
    assert all(c["max_spawn"] == 2 for c in calls)


@pytest.mark.asyncio
async def test_loop_recovers_from_tick_errors(tmp_path: Path, monkeypatch):
    """A broken DB shouldn't crash the loop — it should back off + retry."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    calls = {"n": 0}

    def boom(*_, **__):
        calls["n"] += 1
        raise RuntimeError("simulated db unavailable")

    loop = KanbanDispatcherLoop(interval_seconds=0.05, max_spawn=1)
    with patch("opencomputer.kanban.db.dispatch_once", boom), \
         patch("opencomputer.kanban.db.connect", side_effect=boom):
        task = asyncio.create_task(loop.run_forever())
        await asyncio.sleep(0.20)
        await loop.stop()
        await asyncio.wait_for(task, timeout=3.0)
    # At least one tick attempted; the loop didn't crash (we got here).
    assert calls["n"] >= 1


@pytest.mark.asyncio
async def test_loop_stop_is_prompt(tmp_path: Path, monkeypatch):
    """Calling stop() should wake the loop within ~1s even on a long tick interval."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    class _FakeResult:
        spawned = []
        crashed = []
        timed_out = []
        auto_blocked = []
        promoted = 0
        reclaimed = 0

    class _FakeConnCM:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_connect(): return _FakeConnCM()
    def fake_dispatch_once(*_, **__): return _FakeResult()

    loop = KanbanDispatcherLoop(interval_seconds=60.0, max_spawn=1)
    with patch("opencomputer.kanban.db.dispatch_once", fake_dispatch_once), \
         patch("opencomputer.kanban.db.connect", fake_connect):
        task = asyncio.create_task(loop.run_forever())
        await asyncio.sleep(0.05)
        await loop.stop()
        # Should exit promptly even though tick is 60s
        await asyncio.wait_for(task, timeout=1.0)


# ---- Gateway integration: loop NOT started when disabled ----


@pytest.mark.asyncio
async def test_gateway_does_not_start_loop_when_disabled(tmp_path: Path, monkeypatch):
    """Plant config.yaml with dispatch_in_gateway: false; the gateway
    must skip starting the loop."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("kanban:\n  dispatch_in_gateway: false\n")

    from unittest.mock import MagicMock

    from opencomputer.gateway.server import Gateway

    # Gateway requires loop= OR router= — supply a dummy.
    g = Gateway(loop=MagicMock())
    await g._start_kanban_dispatcher_loop()
    assert g._kanban_dispatcher is None
    assert g._kanban_dispatcher_task is None
