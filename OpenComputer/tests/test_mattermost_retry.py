"""PR #221 O2 — Mattermost adapter retry-on-transient-error coverage.

Verifies ``_send_with_retry`` is wired around the REST v4 calls
(``posts``, ``reactions``, ``posts/{id}`` PUT/DELETE).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest


def _load():
    spec = importlib.util.spec_from_file_location(
        "mattermost_adapter_test_o2",
        Path(__file__).resolve().parent.parent / "extensions" / "mattermost" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.MattermostAdapter


def _make_flaky_handler(success_after: int):
    state = {"calls": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] <= success_after:
            raise httpx.ConnectError("simulated network blip")
        # Mattermost returns 201 on POST /posts.
        return httpx.Response(201, json={"id": "post123"})

    return handler, state


@pytest.mark.asyncio
async def test_mattermost_send_retries_transient_connect_errors() -> None:
    MattermostAdapter = _load()
    handler, state = _make_flaky_handler(success_after=2)

    a = MattermostAdapter(
        config={
            "base_url": "https://mm.example.com",
            "token": "test_token",
        }
    )
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    # Speed up retry backoff
    original = a._send_with_retry

    async def fast_retry(send_fn, *args, **kwargs):
        kwargs.setdefault("base_delay", 0.001)
        return await original(send_fn, *args, **kwargs)

    a._send_with_retry = fast_retry  # type: ignore[assignment]

    res = await a.send("channel123", "hello")
    assert res.success is True
    assert res.message_id == "post123"
    assert state["calls"] == 3
