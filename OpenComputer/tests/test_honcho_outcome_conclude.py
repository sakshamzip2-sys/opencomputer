"""Tests for HonchoSelfHostedProvider.subscribe_to_outcome_events.

Pre-2026-05-11: the handler only wrote a log line. As of 2026-05-11 it
POSTs ``/v1/conclude`` with ``observation_mode=inferred`` so Honcho's
user-model accumulates inferred behavioral signals from every turn.

We verify:
* a TurnCompletedEvent with non-empty signals triggers a conclude POST
* signals are rendered into a single ``fact`` string, sorted+joined
* the fact is capped at 480 chars
* an empty-signals event is skipped (no POST)
* a non-2xx response is logged but never crashes the bus handler
* the HTTP client being closed is handled gracefully (DEBUG log, no POST)
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from pathlib import Path

import httpx
import pytest

from plugin_sdk.ingestion import TurnCompletedEvent

_PROVIDER_PATH = (
    Path(__file__).resolve().parent.parent
    / "extensions"
    / "memory-honcho"
    / "provider.py"
)


def _load_provider_module():
    cache_key = "memory_honcho_provider_outcome_test"
    if cache_key in sys.modules:
        return sys.modules[cache_key]
    spec = importlib.util.spec_from_file_location(cache_key, _PROVIDER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[cache_key] = mod
    spec.loader.exec_module(mod)
    return mod


_provider_mod = _load_provider_module()
HonchoConfig = _provider_mod.HonchoConfig
HonchoSelfHostedProvider = _provider_mod.HonchoSelfHostedProvider


class _StubBus:
    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}

    def subscribe(self, event_type: str, handler):  # noqa: ANN001
        self.handlers.setdefault(event_type, []).append(handler)

        class _Sub:
            def unsubscribe(_):  # noqa: N805, ARG002
                self.handlers[event_type].remove(handler)

        return _Sub()


def _build_provider(handler_fn):
    cfg = HonchoConfig(base_url="http://test.local")
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler_fn),
        base_url=cfg.base_url,
    )
    return HonchoSelfHostedProvider(config=cfg, http_client=client)


def _flush_async_tasks():
    """Drain the asyncio loop so fire-and-forget tasks finish."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            return
        loop.run_until_complete(asyncio.sleep(0.05))
    except RuntimeError:
        # No loop bound to this thread — fire-and-forget tasks ran
        # synchronously inline (test environment).
        pass


def test_conclude_called_on_turn_completed_event():
    """Standard event with signals → POST /v1/conclude with inferred mode."""
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/conclude":
            import json

            captured.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    provider = _build_provider(handler)
    bus = _StubBus()
    sub = provider.subscribe_to_outcome_events(bus)

    # Need to drive the handler inside a running event loop because
    # fire_and_forget schedules on the active loop.
    async def _run():
        bus.handlers["turn_completed"][0](
            TurnCompletedEvent(
                session_id="sess-abc",
                turn_index=3,
                signals={"cost": 0.02, "tool_calls": 5, "errors": 0},
            )
        )
        await asyncio.sleep(0.05)

    asyncio.run(_run())

    assert len(captured) == 1
    payload = captured[0]
    assert payload["observation_mode"] == "inferred"
    assert payload["peer"] == "user"
    assert "Turn 3" in payload["fact"]
    # Signals sorted alphabetically: cost, errors, tool_calls.
    assert payload["fact"].index("cost=") < payload["fact"].index("errors=")
    sub.unsubscribe()


def test_conclude_skipped_on_empty_signals():
    """Event with no signals → no HTTP call."""
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.path)
        return httpx.Response(200)

    provider = _build_provider(handler)
    bus = _StubBus()
    provider.subscribe_to_outcome_events(bus)

    async def _run():
        bus.handlers["turn_completed"][0](
            TurnCompletedEvent(
                session_id="sess", turn_index=1, signals={}
            )
        )
        await asyncio.sleep(0.05)

    asyncio.run(_run())
    assert captured == []


def test_conclude_caps_fact_at_480_chars():
    """A very-large signals dict produces a fact truncated to ≤480 chars."""
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/conclude":
            import json

            captured.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    provider = _build_provider(handler)
    bus = _StubBus()
    provider.subscribe_to_outcome_events(bus)

    # 100 signals each named ``signal_N=...`` → way over 480 chars.
    big_signals = {f"signal_{i:03d}": f"value_with_data_{i}" for i in range(100)}

    async def _run():
        bus.handlers["turn_completed"][0](
            TurnCompletedEvent(
                session_id="sess", turn_index=1, signals=big_signals
            )
        )
        await asyncio.sleep(0.05)

    asyncio.run(_run())

    assert len(captured) == 1
    fact = captured[0]["fact"]
    assert len(fact) <= 480
    # Truncation marker present.
    assert fact.endswith("…")


def test_conclude_handler_survives_4xx():
    """A 400 response → handler logs WARNING but does not raise."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad"})

    provider = _build_provider(handler)
    bus = _StubBus()
    provider.subscribe_to_outcome_events(bus)

    async def _run():
        # Handler must not raise.
        bus.handlers["turn_completed"][0](
            TurnCompletedEvent(
                session_id="s", turn_index=1, signals={"k": "v"}
            )
        )
        await asyncio.sleep(0.05)

    asyncio.run(_run())  # no exception = pass


def test_conclude_handler_survives_network_error():
    """Connection error → handler logs WARNING but does not raise."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    provider = _build_provider(handler)
    bus = _StubBus()
    provider.subscribe_to_outcome_events(bus)

    async def _run():
        bus.handlers["turn_completed"][0](
            TurnCompletedEvent(
                session_id="s", turn_index=1, signals={"k": "v"}
            )
        )
        await asyncio.sleep(0.05)

    asyncio.run(_run())


def test_conclude_skipped_on_closed_client():
    """Closed httpx client → handler logs DEBUG and returns; no POST attempt."""
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.path)
        return httpx.Response(200)

    provider = _build_provider(handler)
    bus = _StubBus()
    provider.subscribe_to_outcome_events(bus)

    async def _run():
        await provider._client.aclose()
        bus.handlers["turn_completed"][0](
            TurnCompletedEvent(
                session_id="s", turn_index=1, signals={"k": "v"}
            )
        )
        await asyncio.sleep(0.05)

    asyncio.run(_run())
    assert captured == []
