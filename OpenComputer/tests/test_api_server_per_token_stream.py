"""Tests for E.2 — per-token SSE streaming on api-server.

The OpenAI-compat ``/v1/chat/completions`` endpoint emits one SSE chunk
per token when ``stream=True`` AND a streaming_handler is registered.
Falls back to the legacy single-chunk path when only the synchronous
handler is set.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer


def _load_adapter():
    spec = importlib.util.spec_from_file_location(
        "api_server_adapter_e2",
        Path(__file__).resolve().parent.parent
        / "extensions"
        / "api-server"
        / "adapter.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_adapter():
    mod = _load_adapter()
    return mod.APIServerAdapter(
        config={"host": "127.0.0.1", "port": 0, "token": "tok"}
    )


def _parse_sse_chunks(raw: bytes) -> list[dict]:
    """Pull JSON payloads out of a multi-event SSE blob, skipping [DONE]."""
    out: list[dict] = []
    for block in raw.decode("utf-8").split("\n\n"):
        if not block.strip():
            continue
        for line in block.splitlines():
            if not line.startswith("data: "):
                continue
            payload = line[len("data: "):]
            if payload == "[DONE]":
                continue
            out.append(json.loads(payload))
    return out


# ─── streaming_handler emits one SSE chunk per delta ──────────────────


@pytest.mark.asyncio
async def test_per_token_streaming_emits_one_chunk_per_delta():
    adapter = _make_adapter()

    deltas = ["Hello", " world", "!"]

    async def streaming_handler(session_id, user_text, on_delta):
        for d in deltas:
            await on_delta(d)

    adapter.set_streaming_handler(streaming_handler)

    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer tok"},
            json={
                "model": "opencomputer",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        assert resp.status == 200
        body = await resp.read()

    chunks = _parse_sse_chunks(body)
    # 3 delta chunks + 1 final-stop chunk = 4 JSON-bearing chunks
    assert len(chunks) == 4
    contents = [
        c["choices"][0]["delta"].get("content", "")
        for c in chunks
        if c["choices"][0]["delta"]
    ]
    assert contents == ["Hello", " world", "!"]
    # Last chunk has finish_reason=stop with empty delta
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["choices"][0]["delta"] == {}


# ─── DONE terminator at end of stream ─────────────────────────────────


@pytest.mark.asyncio
async def test_streaming_response_ends_with_done_marker():
    adapter = _make_adapter()

    async def streaming_handler(session_id, user_text, on_delta):
        await on_delta("a")

    adapter.set_streaming_handler(streaming_handler)

    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer tok"},
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        body = await resp.read()
    assert b"data: [DONE]\n\n" in body


# ─── Empty deltas don't emit empty chunks ─────────────────────────────


@pytest.mark.asyncio
async def test_empty_delta_does_not_emit_chunk():
    adapter = _make_adapter()

    async def streaming_handler(session_id, user_text, on_delta):
        await on_delta("")  # empty — should be filtered
        await on_delta("real")

    adapter.set_streaming_handler(streaming_handler)

    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer tok"},
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        body = await resp.read()

    chunks = _parse_sse_chunks(body)
    contents = [
        c["choices"][0]["delta"].get("content", "")
        for c in chunks
        if c["choices"][0]["delta"]
    ]
    assert contents == ["real"]


# ─── Streaming handler error mid-stream → error chunk + DONE ──────────


@pytest.mark.asyncio
async def test_streaming_error_emits_error_chunk_then_done():
    adapter = _make_adapter()

    async def streaming_handler(session_id, user_text, on_delta):
        await on_delta("first")
        raise RuntimeError("boom")

    adapter.set_streaming_handler(streaming_handler)

    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer tok"},
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        # Note: status was already 200 before the error.
        assert resp.status == 200
        body = await resp.read()

    text = body.decode("utf-8")
    assert "boom" in text
    assert "data: [DONE]\n\n" in text


# ─── Fallback to legacy single-chunk when no streaming_handler ────────


@pytest.mark.asyncio
async def test_falls_back_to_single_chunk_when_streaming_handler_missing():
    adapter = _make_adapter()

    async def sync_handler(session_id, user_text):
        return "full reply"

    adapter.set_handler(sync_handler)
    # NOTE: NOT calling set_streaming_handler — fallback path expected.

    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer tok"},
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        assert resp.status == 200
        body = await resp.read()

    chunks = _parse_sse_chunks(body)
    # 1 delta chunk with full reply + 1 final = 2
    assert len(chunks) == 2
    assert chunks[0]["choices"][0]["delta"]["content"] == "full reply"


# ─── Streaming handler not configured AND no legacy handler → 503 ─────


@pytest.mark.asyncio
async def test_no_handler_at_all_returns_503():
    adapter = _make_adapter()
    # No handlers registered at all.

    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer tok"},
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        assert resp.status == 503
