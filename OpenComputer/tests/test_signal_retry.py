"""PR #221 O2 — Signal adapter retry-on-transient-error coverage.

Verifies that ``self._send_with_retry`` is wired around the signal-cli
JSON-RPC POST: 2 ConnectErrors followed by a 200 OK results in a single
final ``SendResult(success=True)``.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest


def _load():
    spec = importlib.util.spec_from_file_location(
        "signal_adapter_test_o2",
        Path(__file__).resolve().parent.parent / "extensions" / "signal" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SignalAdapter


def _make_flaky_handler(success_after: int):
    """Return an httpx MockTransport handler that raises ConnectError
    *success_after* times, then responds 200 OK with a JSON-RPC success.
    """
    state = {"calls": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] <= success_after:
            raise httpx.ConnectError("simulated network blip")
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "send",
                "result": {"timestamp": 1714000000000},
            },
        )

    return handler, state


@pytest.mark.asyncio
async def test_signal_send_retries_transient_connect_errors() -> None:
    SignalAdapter = _load()
    handler, state = _make_flaky_handler(success_after=2)

    a = SignalAdapter(
        config={
            "signal_cli_url": "http://localhost:8080",
            "phone_number": "+15551234567",
        }
    )
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # Speed up the retry backoff in tests; otherwise default is 1s base.
    original = a._send_with_retry

    async def fast_retry(send_fn, *args, **kwargs):
        kwargs.setdefault("base_delay", 0.001)
        return await original(send_fn, *args, **kwargs)

    a._send_with_retry = fast_retry  # type: ignore[assignment]

    res = await a.send("+19998887777", "hello")
    assert res.success is True
    # 2 transient failures + 1 success = 3 calls
    assert state["calls"] == 3


@pytest.mark.asyncio
async def test_signal_send_reaction_retries_transient_connect_errors() -> None:
    SignalAdapter = _load()
    handler, state = _make_flaky_handler(success_after=2)

    a = SignalAdapter(
        config={
            "signal_cli_url": "http://localhost:8080",
            "phone_number": "+15551234567",
        }
    )
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    original = a._send_with_retry

    async def fast_retry(send_fn, *args, **kwargs):
        kwargs.setdefault("base_delay", 0.001)
        return await original(send_fn, *args, **kwargs)

    a._send_with_retry = fast_retry  # type: ignore[assignment]

    res = await a.send_reaction("+19998887777", "1714000000000", "👍")
    assert res.success is True
    assert state["calls"] == 3
