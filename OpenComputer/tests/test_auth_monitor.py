"""Tests for auth_monitor_once + auth_monitor_loop (OpenClaw 1.E).

Thin surface over the existing per-key quarantine in
``opencomputer/agent/credential_pool.py``. The monitor must NOT add a new
``cooldown(profile_id, ...)`` API — it routes through the existing
``report_auth_failure(key, reason=...)`` per AMENDMENTS Fix C3.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from opencomputer.agent.credential_pool import CredentialPool
from opencomputer.doctor import auth_monitor_loop, auth_monitor_once


class _FakeProvider:
    """Stub provider with credential_pool + ping(key)."""

    def __init__(self, *, keys: list[str], failing_keys: set[str] | None = None):
        self.credential_pool = CredentialPool(keys=keys)
        self._failing = failing_keys or set()
        self.ping_calls: list[str] = []

    async def ping(self, key: str) -> None:
        self.ping_calls.append(key)
        if key in self._failing:
            raise RuntimeError("simulated health-check failure")


class _NoPingProvider:
    def __init__(self, *, keys: list[str]):
        self.credential_pool = CredentialPool(keys=keys)


@pytest.mark.asyncio
async def test_auth_monitor_once_demotes_failing_key():
    p = _FakeProvider(keys=["sk-A", "sk-B", "sk-C"], failing_keys={"sk-B"})
    report = await auth_monitor_once(providers={"anthropic": p})
    assert report["anthropic"]["sk-A..."] == "ok"
    assert report["anthropic"]["sk-B..."].startswith("failed:")
    assert report["anthropic"]["sk-C..."] == "ok"

    # Verify the existing quarantine kicked in via report_auth_failure
    states = {s.key: s for s in p.credential_pool._states}
    import time

    now = time.time()
    assert states["sk-B"].quarantined_until > now, "failing key must be quarantined"
    assert states["sk-A"].quarantined_until <= now, "healthy key must NOT be quarantined"
    assert states["sk-C"].quarantined_until <= now


@pytest.mark.asyncio
async def test_auth_monitor_once_pings_all_keys_in_order():
    p = _FakeProvider(keys=["k1", "k2", "k3"])
    await auth_monitor_once(providers={"openai": p})
    assert p.ping_calls == ["k1", "k2", "k3"]


@pytest.mark.asyncio
async def test_auth_monitor_once_no_ping_provider_emits_warning(caplog):
    p = _NoPingProvider(keys=["k1"])
    with caplog.at_level(logging.WARNING):
        report = await auth_monitor_once(providers={"openai": p})
    assert report["openai"]["__error__"] == "no ping method"
    assert any("no ping()" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_auth_monitor_once_no_pool_provider_skipped():
    class _BareProvider: ...

    bare = _BareProvider()
    report = await auth_monitor_once(providers={"weird": bare})
    assert report["weird"]["__error__"] == "no credential_pool"


@pytest.mark.asyncio
async def test_auth_monitor_once_does_not_invent_cooldown_method():
    """Audit Fix C3 — must NOT call CredentialPool.cooldown(profile_id, seconds)."""
    p = _FakeProvider(keys=["k1"])
    # Sanity: real pool has no `cooldown` method; routing through it would AttributeError.
    assert not hasattr(p.credential_pool, "cooldown")
    await auth_monitor_once(providers={"x": p})  # must not raise


@pytest.mark.asyncio
async def test_auth_monitor_loop_exits_on_stop_event():
    p = _FakeProvider(keys=["k1"])
    stop = asyncio.Event()

    async def stop_soon():
        await asyncio.sleep(0.05)
        stop.set()

    async with asyncio.TaskGroup() as tg:
        tg.create_task(stop_soon())
        tg.create_task(
            auth_monitor_loop(
                providers={"a": p},
                interval_seconds=10,  # much longer than stop_soon delay
                stop_event=stop,
            )
        )
    # If we get here, the loop exited promptly (didn't wait the full 10s).
    assert len(p.ping_calls) >= 1, "first pass must run before stop"


@pytest.mark.asyncio
async def test_auth_monitor_loop_handles_pass_exception_then_continues():
    """If a pass raises, the loop logs and continues to next interval."""
    p = _FakeProvider(keys=["k1"])
    stop = asyncio.Event()

    call_count = [0]
    real_ping = p.ping

    async def flaky_ping(key):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("first pass blows up")
        await real_ping(key)

    p.ping = flaky_ping

    async def stop_after_two_passes():
        # Wait until 2 ping attempts done, then stop
        while call_count[0] < 2:
            await asyncio.sleep(0.01)
        stop.set()

    async with asyncio.TaskGroup() as tg:
        tg.create_task(stop_after_two_passes())
        tg.create_task(
            auth_monitor_loop(
                providers={"a": p},
                interval_seconds=0,  # tight loop for test
                stop_event=stop,
            )
        )
    assert call_count[0] >= 2, "loop must continue past first-pass exception"
