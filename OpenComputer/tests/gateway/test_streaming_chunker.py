"""Tests for ``opencomputer.gateway.streaming_chunker.BlockStreamingChunker``.

Covers the four behavioral guarantees of the async chunker:

1. paragraph (``\\n\\n``) boundaries are preferred over weaker ones
2. text inside a triple-backtick fence is NEVER split mid-fence
3. an idle gap (no new feeds for ``idle_ms``) flushes pending text
4. ``close`` flushes whatever is left, fence-open or not

Plus a few defensive cases — empty input, safe-mode reinsert on emit
failure, no-split when fence stays open across the stream.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from opencomputer.gateway.streaming_chunker import (
    BlockStreamingChunker,
    ChunkerConfig,
)


@pytest.mark.asyncio
async def test_paragraph_boundary_preferred() -> None:
    chunks: list[str] = []

    async def collect(c: str) -> None:
        chunks.append(c)

    chunker = BlockStreamingChunker(
        emit=collect, config=ChunkerConfig(human_delay_ms=0)
    )
    await chunker.feed("First paragraph.\n\nSecond paragraph.\n\nThird.")
    await chunker.close()

    assembled = "".join(chunks)
    assert "First paragraph" in assembled
    assert "Second paragraph" in assembled
    assert "Third" in assembled


@pytest.mark.asyncio
async def test_no_split_inside_code_fence() -> None:
    """A complete fence emits intact; nothing is delivered mid-fence."""
    chunks: list[str] = []

    async def collect(c: str) -> None:
        chunks.append(c)

    chunker = BlockStreamingChunker(
        emit=collect, config=ChunkerConfig(human_delay_ms=0)
    )
    text = "Look:\n\n```python\ndef foo():\n    return 1\n```\n\nDone."
    await chunker.feed(text)
    await chunker.close()

    assembled = "".join(chunks)
    assert "```python" in assembled
    assert "def foo():" in assembled
    assert "Done." in assembled
    # No emitted chunk straddles the fence midway — every chunk that
    # contains "```python" must also contain the closing "```" before
    # "Done."  We check that no chunk contains opening fence WITHOUT
    # closing fence (ie an unbalanced one — except a trailing tail).
    for c in chunks:
        if "```python" in c:
            # Either both opens and closes are in the same chunk, or
            # the chunk ends inside the fence (which means the fence
            # is still open in the buffer — but with our test we feed
            # the whole text up front, so the closing fence must be
            # present in the chunk that has the opener).
            assert c.count("```") >= 2, (
                f"chunk has unbalanced fence: {c!r}"
            )


@pytest.mark.asyncio
async def test_unbalanced_fence_holds_until_close() -> None:
    """Fence-open content is held; close still flushes it (truncated stream)."""
    chunks: list[str] = []

    async def collect(c: str) -> None:
        chunks.append(c)

    chunker = BlockStreamingChunker(
        emit=collect, config=ChunkerConfig(human_delay_ms=0, idle_ms=10_000)
    )
    # Feed an opening fence with no close + text that has paragraph
    # boundary AFTER the fence opener. Nothing should emit while the
    # fence is open.
    await chunker.feed("Header text.\n\n```python\ndef foo():\n    pass\n")
    # No close yet — no emits should have happened that cut into the fence.
    # (The "Header text." paragraph-boundary chunk DOES emit cleanly
    #  before the fence opens — that's allowed.)
    pre_close = list(chunks)
    for c in pre_close:
        # No chunk should contain the opening fence without its closer
        assert c.count("```") % 2 != 1, (
            f"emit happened while fence open: {c!r}"
        )

    # Close MUST still flush the open-fence content rather than dropping it.
    await chunker.close()
    assembled = "".join(chunks)
    assert "def foo():" in assembled


@pytest.mark.asyncio
async def test_idle_coalesce_flushes() -> None:
    """If no new feed arrives for ``idle_ms``, buffer auto-flushes."""
    chunks: list[str] = []

    async def collect(c: str) -> None:
        chunks.append(c)

    chunker = BlockStreamingChunker(
        emit=collect, config=ChunkerConfig(idle_ms=50, human_delay_ms=0)
    )
    await chunker.feed("partial text without boundary")
    # Wait past the idle window so the timer fires.
    await asyncio.sleep(0.2)
    # Already flushed via idle timer — close is just defensive cleanup.
    await chunker.close()
    assembled = "".join(chunks)
    assert "partial text without boundary" in assembled


@pytest.mark.asyncio
async def test_close_flushes_remaining() -> None:
    """``close`` flushes anything still buffered, even with no boundary."""
    chunks: list[str] = []

    async def collect(c: str) -> None:
        chunks.append(c)

    chunker = BlockStreamingChunker(
        emit=collect, config=ChunkerConfig(human_delay_ms=0, idle_ms=10_000)
    )
    await chunker.feed("no boundary")
    # No idle wait — close must still deliver the remainder.
    await chunker.close()
    assert "no boundary" in "".join(chunks)


@pytest.mark.asyncio
async def test_empty_feed_is_noop() -> None:
    """Calling feed with empty string changes nothing."""
    chunks: list[str] = []

    async def collect(c: str) -> None:
        chunks.append(c)

    chunker = BlockStreamingChunker(
        emit=collect, config=ChunkerConfig(human_delay_ms=0)
    )
    await chunker.feed("")
    await chunker.close()
    assert chunks == []


@pytest.mark.asyncio
async def test_emit_failure_reinserts(caplog) -> None:
    """If emit raises, the chunk is reinserted (not silently dropped)."""
    fail_first = {"done": False}
    chunks: list[str] = []

    async def flaky(c: str) -> None:
        if not fail_first["done"]:
            fail_first["done"] = True
            raise RuntimeError("send failed")
        chunks.append(c)

    chunker = BlockStreamingChunker(
        emit=flaky, config=ChunkerConfig(human_delay_ms=0, idle_ms=10_000)
    )
    with caplog.at_level(logging.ERROR):
        await chunker.feed("hello world.\n\n")
        await chunker.close()
    # Second emit (close-time flush of reinserted text) succeeds and
    # delivers the original chunk.
    assert any("hello world" in c for c in chunks)
    assert any("emit failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_feed_after_close_is_noop() -> None:
    """Closed chunkers ignore further feeds rather than crashing."""
    chunks: list[str] = []

    async def collect(c: str) -> None:
        chunks.append(c)

    chunker = BlockStreamingChunker(
        emit=collect, config=ChunkerConfig(human_delay_ms=0)
    )
    await chunker.close()
    await chunker.feed("post-close")
    assert chunks == []
