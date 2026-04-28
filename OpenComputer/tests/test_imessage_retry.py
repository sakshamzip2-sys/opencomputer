"""PR #221 O2 — iMessage adapter retry-on-transient-error coverage.

Verifies ``_send_with_retry`` is wired around the BlueBubbles bridge
POSTs (``/message/text``, ``/message/react``).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest


def _load():
    spec = importlib.util.spec_from_file_location(
        "imessage_adapter_test_o2",
        Path(__file__).resolve().parent.parent / "extensions" / "imessage" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.IMessageAdapter


def _make_flaky_handler(success_after: int):
    state = {"calls": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] <= success_after:
            raise httpx.ConnectError("simulated network blip")
        return httpx.Response(200, json={"status": 200, "data": {}})

    return handler, state


@pytest.mark.asyncio
async def test_imessage_send_retries_transient_connect_errors() -> None:
    IMessageAdapter = _load()
    handler, state = _make_flaky_handler(success_after=2)

    a = IMessageAdapter(
        config={
            "base_url": "http://localhost:1234",
            "password": "test_password",
        }
    )
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    # Speed up retry backoff so the test doesn't sleep for ~3 seconds.
    original = a._send_with_retry

    async def fast_retry(send_fn, *args, **kwargs):
        kwargs.setdefault("base_delay", 0.001)
        return await original(send_fn, *args, **kwargs)

    a._send_with_retry = fast_retry  # type: ignore[assignment]

    res = await a.send("iMessage;-;+15551234567", "hi")
    assert res.success is True
    assert state["calls"] == 3
