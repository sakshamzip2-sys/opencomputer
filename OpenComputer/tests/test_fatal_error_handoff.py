"""Tests for adapter fatal-error handoff (Hermes PR 2 Task 2.3 + amendment §A.5).

Adapters call ``_set_fatal_error(code, message, retryable=...)`` when a
non-recoverable condition is detected (Telegram conflict from a parallel
poller, expired credentials, etc.). The gateway's periodic supervisor
inspects each adapter; on retryable=True it cycles disconnect →
``clear_fatal_error()`` → connect; on retryable=False it logs ERROR and
leaves the adapter disconnected.

Per amendment §A.5, the gateway uses ``clear_fatal_error()`` rather than
mutating private fields directly — keeps encapsulation honest.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from plugin_sdk.channel_contract import BaseChannelAdapter
from plugin_sdk.core import Platform


class _MinAdapter(BaseChannelAdapter):
    platform = Platform.CLI

    def __init__(self, config) -> None:
        super().__init__(config)
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connect(self) -> bool:
        self.connect_calls += 1
        return True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def send(self, *a, **kw):
        return None


# ─── BaseChannelAdapter contract ─────────────────────────────────────


def test_set_fatal_error_records_state() -> None:
    a = _MinAdapter({})
    a._set_fatal_error("conflict", "another process polling", retryable=False)
    assert a._fatal_error_code == "conflict"
    assert a._fatal_error_message == "another process polling"
    assert a._fatal_error_retryable is False
    assert a.has_fatal_error()


def test_set_fatal_error_retryable_true() -> None:
    a = _MinAdapter({})
    a._set_fatal_error("network", "transport down", retryable=True)
    assert a._fatal_error_retryable is True


def test_no_fatal_error_initial_state() -> None:
    a = _MinAdapter({})
    assert a._fatal_error_code is None
    assert a._fatal_error_message is None
    assert a._fatal_error_retryable is False
    assert not a.has_fatal_error()


def test_clear_fatal_error_resets_state() -> None:
    """Per amendment §A.5: gateway calls clear_fatal_error() (no direct mutation)."""
    a = _MinAdapter({})
    a._set_fatal_error("network", "down", retryable=True)
    assert a.has_fatal_error()
    a.clear_fatal_error()
    assert not a.has_fatal_error()
    assert a._fatal_error_code is None
    assert a._fatal_error_message is None
    assert a._fatal_error_retryable is False


def test_set_fatal_error_logs_at_error_level(caplog) -> None:
    a = _MinAdapter({})
    with caplog.at_level(logging.ERROR, logger="plugin_sdk.channel_contract"):
        a._set_fatal_error("auth_failed", "invalid token", retryable=False)
    assert any(
        "fatal error" in rec.message.lower() for rec in caplog.records
    ), f"expected fatal-error log line; got: {[r.message for r in caplog.records]}"


# ─── Gateway supervisor (Step 2.3.1 / §A.5) ──────────────────────────


@pytest.mark.asyncio
async def test_gateway_supervisor_reconnects_retryable_adapter() -> None:
    """Supervisor sees retryable fatal → disconnect → clear → connect."""
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.gateway.server import Gateway

    loop = AgentLoop.__new__(AgentLoop)  # bypass real ctor
    gw = Gateway.__new__(Gateway)
    gw.loop = loop  # type: ignore[attr-defined]
    gw._adapters = []  # type: ignore[attr-defined]

    a = _MinAdapter({})
    a._set_fatal_error("network", "reset", retryable=True)
    gw._adapters.append(a)  # type: ignore[attr-defined]

    # Tick once with a tiny interval so the test doesn't take 60s.
    await gw._tick_fatal_error_supervisor()

    assert a.disconnect_calls == 1, "supervisor should have called disconnect"
    assert a.connect_calls == 1, "supervisor should have reconnected"
    assert not a.has_fatal_error(), "fatal-error state should be cleared"


@pytest.mark.asyncio
async def test_gateway_supervisor_logs_non_retryable_only(caplog) -> None:
    """Non-retryable fatal: ERROR log only, NO disconnect/connect cycle."""
    from opencomputer.gateway.server import Gateway

    gw = Gateway.__new__(Gateway)
    gw._adapters = []  # type: ignore[attr-defined]

    a = _MinAdapter({})
    a._set_fatal_error("conflict", "parallel poller", retryable=False)
    gw._adapters.append(a)  # type: ignore[attr-defined]

    with caplog.at_level(logging.ERROR, logger="opencomputer.gateway.server"):
        await gw._tick_fatal_error_supervisor()

    assert a.disconnect_calls == 0
    assert a.connect_calls == 0
    assert a.has_fatal_error(), (
        "non-retryable fatal must NOT auto-clear — only manual recovery"
    )
    assert any("conflict" in rec.message for rec in caplog.records), (
        "expected 'conflict' in log line"
    )


@pytest.mark.asyncio
async def test_gateway_supervisor_skips_healthy_adapters() -> None:
    """No fatal flag → supervisor leaves the adapter alone."""
    from opencomputer.gateway.server import Gateway

    gw = Gateway.__new__(Gateway)
    gw._adapters = []  # type: ignore[attr-defined]

    a = _MinAdapter({})
    gw._adapters.append(a)  # type: ignore[attr-defined]

    await gw._tick_fatal_error_supervisor()
    assert a.disconnect_calls == 0
    assert a.connect_calls == 0


@pytest.mark.asyncio
async def test_gateway_supervisor_swallows_reconnect_failure(caplog) -> None:
    """If disconnect/connect raises, supervisor logs and continues."""
    from opencomputer.gateway.server import Gateway

    class _BrokenReconnect(_MinAdapter):
        async def disconnect(self) -> None:
            raise RuntimeError("boom")

    gw = Gateway.__new__(Gateway)
    gw._adapters = []  # type: ignore[attr-defined]
    a = _BrokenReconnect({})
    a._set_fatal_error("network", "down", retryable=True)
    gw._adapters.append(a)  # type: ignore[attr-defined]

    # Should NOT raise.
    with caplog.at_level(logging.ERROR, logger="opencomputer.gateway.server"):
        await gw._tick_fatal_error_supervisor()

    # State left as-is (still flagged) since recovery failed.
    assert a.has_fatal_error()


@pytest.mark.asyncio
async def test_gateway_supervisor_loop_breaks_on_stop() -> None:
    """The 60s loop wakes promptly when the stop event fires."""
    from opencomputer.gateway.server import Gateway

    gw = Gateway.__new__(Gateway)
    gw._adapters = []  # type: ignore[attr-defined]
    gw._fatal_supervisor_stop = asyncio.Event()  # type: ignore[attr-defined]
    # Use a tiny interval so the test runs in milliseconds.
    task = asyncio.create_task(
        gw._check_fatal_errors_periodic(interval=0.05)
    )
    await asyncio.sleep(0.1)  # let the loop tick at least once
    gw._fatal_supervisor_stop.set()  # type: ignore[attr-defined]
    await asyncio.wait_for(task, timeout=1.0)


# ─── Startup connect()-False handoff (2026-05-08 incident) ───────────


class _ConnectFalseAdapter(_MinAdapter):
    """Adapter whose first connect() returns False — simulates Telegram
    bot-token already-held-by-other-process at startup."""

    def __init__(self, config) -> None:
        super().__init__(config)
        self._first_call = True

    async def connect(self) -> bool:
        self.connect_calls += 1
        if self._first_call:
            self._first_call = False
            return False
        return True


class _ConnectRaisesAdapter(_MinAdapter):
    """Adapter whose first connect() raises an exception."""

    def __init__(self, config) -> None:
        super().__init__(config)
        self._first_call = True

    async def connect(self) -> bool:
        self.connect_calls += 1
        if self._first_call:
            self._first_call = False
            raise RuntimeError("transient transport error at startup")
        return True


@pytest.mark.asyncio
async def test_startup_connect_false_triggers_fatal_retryable() -> None:
    """Regression for the 2026-05-08 incident.

    When ``adapter.connect()`` returns ``False`` at gateway startup,
    the gateway must mark the adapter ``_set_fatal_error(...,
    retryable=True)`` so the periodic fatal-error supervisor reconnects
    on the next tick. Pre-fix, server.py:236 just logged "returned False
    from connect()" and parked the adapter forever — exactly what
    happened with the Telegram bot polling-slot conflict on 2026-05-08
    (Hermes held the slot, OC's connect-False was silent).
    """
    from opencomputer.gateway.server import Gateway

    gw = Gateway.__new__(Gateway)
    gw.loop = None  # type: ignore[attr-defined]
    gw._adapters = []  # type: ignore[attr-defined]
    gw._drainer = None  # type: ignore[attr-defined]
    gw._drainer_task = None  # type: ignore[attr-defined]
    gw._ambient_daemon = None  # type: ignore[attr-defined]
    gw._evolution_subscriber = None  # type: ignore[attr-defined]
    gw._kanban_dispatcher = None  # type: ignore[attr-defined]
    gw._kanban_dispatcher_task = None  # type: ignore[attr-defined]
    gw._fatal_supervisor_task = None  # type: ignore[attr-defined]
    gw._fatal_supervisor_stop = asyncio.Event()  # type: ignore[attr-defined]

    a = _ConnectFalseAdapter({})
    gw._adapters.append(a)  # type: ignore[attr-defined]

    # Skip the side-effect helpers (drainer, ambient, kanban, etc.)
    # by monkey-patching the start helpers to no-ops. We're isolating
    # the connect() handling.
    async def _noop() -> None:
        pass

    gw._start_outgoing_drainer = _noop  # type: ignore[attr-defined]
    gw._start_ambient_daemon = _noop  # type: ignore[attr-defined]
    gw._start_evolution_subscriber = _noop  # type: ignore[attr-defined]
    gw._start_traces_subscriber = _noop  # type: ignore[attr-defined]
    gw._start_kanban_dispatcher_loop = _noop  # type: ignore[attr-defined]
    gw._fire_startup_pings = _noop  # type: ignore[attr-defined]

    await gw.start()

    # Adapter should be flagged fatal-retryable so the supervisor will
    # try to reconnect on the next 60s tick.
    assert a.has_fatal_error(), (
        "adapter must be flagged fatal after connect() returned False"
    )
    assert a._fatal_error_retryable is True, (
        "fatal must be retryable so the supervisor reconnects"
    )
    assert a._fatal_error_code == "connect_returned_false"

    # Cleanup: stop the supervisor task that gw.start() spawned.
    gw._fatal_supervisor_stop.set()
    if gw._fatal_supervisor_task is not None:
        try:
            await asyncio.wait_for(gw._fatal_supervisor_task, timeout=1.0)
        except (TimeoutError, asyncio.CancelledError):
            gw._fatal_supervisor_task.cancel()


@pytest.mark.asyncio
async def test_startup_connect_exception_triggers_fatal_retryable() -> None:
    """Companion to the connect-False test — an exception during connect
    at startup must also flag fatal-retryable."""
    from opencomputer.gateway.server import Gateway

    gw = Gateway.__new__(Gateway)
    gw.loop = None  # type: ignore[attr-defined]
    gw._adapters = []  # type: ignore[attr-defined]
    gw._drainer = None  # type: ignore[attr-defined]
    gw._drainer_task = None  # type: ignore[attr-defined]
    gw._ambient_daemon = None  # type: ignore[attr-defined]
    gw._evolution_subscriber = None  # type: ignore[attr-defined]
    gw._kanban_dispatcher = None  # type: ignore[attr-defined]
    gw._kanban_dispatcher_task = None  # type: ignore[attr-defined]
    gw._fatal_supervisor_task = None  # type: ignore[attr-defined]
    gw._fatal_supervisor_stop = asyncio.Event()  # type: ignore[attr-defined]

    a = _ConnectRaisesAdapter({})
    gw._adapters.append(a)  # type: ignore[attr-defined]

    async def _noop() -> None:
        pass

    gw._start_outgoing_drainer = _noop  # type: ignore[attr-defined]
    gw._start_ambient_daemon = _noop  # type: ignore[attr-defined]
    gw._start_evolution_subscriber = _noop  # type: ignore[attr-defined]
    gw._start_traces_subscriber = _noop  # type: ignore[attr-defined]
    gw._start_kanban_dispatcher_loop = _noop  # type: ignore[attr-defined]
    gw._fire_startup_pings = _noop  # type: ignore[attr-defined]

    await gw.start()

    assert a.has_fatal_error()
    assert a._fatal_error_retryable is True
    assert a._fatal_error_code == "connect_raised_exception"

    gw._fatal_supervisor_stop.set()
    if gw._fatal_supervisor_task is not None:
        try:
            await asyncio.wait_for(gw._fatal_supervisor_task, timeout=1.0)
        except (TimeoutError, asyncio.CancelledError):
            gw._fatal_supervisor_task.cancel()
