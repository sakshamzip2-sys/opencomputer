"""Tests for ScreenContextProvider — DynamicInjectionProvider that emits
<screen_context> overlay from the ring buffer's latest capture."""
from __future__ import annotations

import asyncio
import time

from plugin_sdk.injection import InjectionContext
from plugin_sdk.runtime_context import DEFAULT_RUNTIME_CONTEXT

from extensions.screen_awareness.injection_provider import ScreenContextProvider
from extensions.screen_awareness.ring_buffer import ScreenCapture, ScreenRingBuffer


def _ctx(session_id: str = "s1") -> InjectionContext:
    return InjectionContext(
        messages=(),
        runtime=DEFAULT_RUNTIME_CONTEXT,
        session_id=session_id,
    )


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_provider_id_is_unique_string():
    provider = ScreenContextProvider(ring_buffer=ScreenRingBuffer(max_size=5))
    assert provider.provider_id == "screen_context"


def test_empty_buffer_returns_empty_string():
    provider = ScreenContextProvider(
        ring_buffer=ScreenRingBuffer(max_size=5)
    )
    assert _run(provider.collect(_ctx())) == ""


def test_latest_capture_emitted_as_screen_context():
    buf = ScreenRingBuffer(max_size=5)
    buf.append(ScreenCapture(
        captured_at=time.time(),
        text="hello world",
        sha256="abc",
        trigger="user_message",
        session_id="s1",
    ))
    provider = ScreenContextProvider(ring_buffer=buf)
    out = _run(provider.collect(_ctx()))
    assert "<screen_context>" in out
    assert "hello world" in out
    assert "</screen_context>" in out


def test_stale_capture_skipped_when_freshness_window_set():
    buf = ScreenRingBuffer(max_size=5)
    buf.append(ScreenCapture(
        captured_at=time.time() - 600,
        text="old",
        sha256="o",
        trigger="user_message",
        session_id="s1",
    ))
    provider = ScreenContextProvider(
        ring_buffer=buf, freshness_seconds=10.0
    )
    assert _run(provider.collect(_ctx())) == ""


def test_text_truncated_to_max_chars():
    buf = ScreenRingBuffer(max_size=5)
    long_text = "x" * 10_000
    buf.append(ScreenCapture(
        captured_at=time.time(),
        text=long_text,
        sha256="big",
        trigger="user_message",
        session_id="s1",
    ))
    provider = ScreenContextProvider(ring_buffer=buf, max_chars=4_000)
    out = _run(provider.collect(_ctx()))
    body = out.split("<screen_context>")[1].split("</screen_context>")[0]
    assert len(body) <= 4_000 + 80  # 80 for ellipsis + metadata line
    assert "…" in body  # truncation marker present
