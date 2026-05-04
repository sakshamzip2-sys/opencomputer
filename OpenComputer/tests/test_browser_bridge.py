"""Layered Awareness MVP — browser-bridge Python listener tests.

Exercises the real aiohttp endpoint round-trip (mock-free for the HTTP
path; only the bus subscriber is custom).
"""
import asyncio

import aiohttp
import pytest


async def test_browser_bridge_accepts_post_and_publishes_event():
    from extensions.browser_bridge.adapter import BrowserBridgeAdapter

    from opencomputer.ingestion.bus import TypedEventBus

    bus = TypedEventBus()
    received: list = []

    def handler(ev) -> None:
        received.append(ev)

    bus.subscribe("browser_visit", handler)

    adapter = BrowserBridgeAdapter(bus=bus, port=18791, token="test-token")
    runner = await adapter.start()
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "url": "https://example.com",
                "title": "Example",
                "visit_time": 1714086400.0,
            }
            async with session.post(
                "http://127.0.0.1:18791/browser-event",
                json=payload,
                headers={"Authorization": "Bearer test-token"},
            ) as resp:
                assert resp.status == 200
        # event bus fanout is sync — give it a tick to settle.
        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0].event_type == "browser_visit"
        assert received[0].metadata["url"] == "https://example.com"
    finally:
        await runner.cleanup()


async def test_browser_bridge_rejects_missing_token():
    from extensions.browser_bridge.adapter import BrowserBridgeAdapter

    from opencomputer.ingestion.bus import TypedEventBus

    bus = TypedEventBus()
    adapter = BrowserBridgeAdapter(bus=bus, port=18792, token="real-token")
    runner = await adapter.start()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "http://127.0.0.1:18792/browser-event",
                json={"url": "x"},
            ) as resp:
                assert resp.status == 401
    finally:
        await runner.cleanup()


async def test_browser_bridge_handles_port_in_use():
    """If the port is already bound, raise a clean OSError with actionable msg."""
    import socket

    from extensions.browser_bridge.adapter import BrowserBridgeAdapter

    from opencomputer.ingestion.bus import TypedEventBus

    # Pick a free ephemeral port at runtime so the test isn't flaky if
    # something else on the dev machine happens to be on a fixed port.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    finally:
        sock.close()

    bus = TypedEventBus()
    a = BrowserBridgeAdapter(bus=bus, port=port, token="t")
    runner_a = await a.start()
    try:
        b = BrowserBridgeAdapter(bus=bus, port=port, token="t")
        # Second bind on same port must raise OSError; we don't want
        # the adapter to silently swallow the bind failure.
        with pytest.raises(OSError):
            await b.start()
    finally:
        await runner_a.cleanup()
