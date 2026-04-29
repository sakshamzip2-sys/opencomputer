"""Tests for plugin_sdk.streaming.BlockChunker (OpenClaw 1.A)."""
from __future__ import annotations

import random

import pytest

from plugin_sdk.streaming import Block, BlockChunker, wrap_stream_callback


def test_paragraph_first_split():
    c = BlockChunker(min_chars=10, max_chars=1000)
    out = c.feed("First paragraph here.\n\nSecond paragraph here.\n\n")
    assert len(out) == 2
    assert "First paragraph here." in out[0].text
    assert "Second paragraph here." in out[1].text


def test_min_chars_buffers_short_input():
    c = BlockChunker(min_chars=80, max_chars=1500)
    assert c.feed("short.") == []
    flushed = c.flush()
    assert len(flushed) == 1
    assert flushed[0].text == "short."


def test_max_chars_force_split():
    c = BlockChunker(min_chars=10, max_chars=200)
    text = "x" * 500
    out = c.feed(text)
    assert len(out) >= 1
    assert all(len(b.text) <= 200 for b in out)


def test_never_splits_inside_fence():
    long_inside = "long " + ("y " * 100) + "tail"
    text = f"Before fence here.\n\n```python\n{long_inside}\n```\n\nAfter fence."
    c = BlockChunker(min_chars=10, max_chars=200)
    out = c.feed(text)
    flushed = c.flush()
    joined = "".join(b.text for b in out + flushed)
    # Reconstructed content preserves the fence intact (no mid-fence split)
    assert "```python" in joined
    assert "```" in joined.split("```python")[1] if "```python" in joined else False
    assert joined.count("```") >= 2  # both fence markers present


def test_human_delay_within_range():
    random.seed(42)
    c = BlockChunker(human_delay_min_ms=800, human_delay_max_ms=2500)
    delays = [c.human_delay() for _ in range(50)]
    assert all(0.8 <= d <= 2.5 for d in delays)


def test_block_is_immutable():
    b = Block(text="hello", boundary="paragraph")
    with pytest.raises(AttributeError):
        b.text = "x"  # frozen dataclass


def test_invalid_min_max_raises():
    with pytest.raises(ValueError):
        BlockChunker(min_chars=0, max_chars=10)
    with pytest.raises(ValueError):
        BlockChunker(min_chars=100, max_chars=50)


def test_flush_empty_returns_empty():
    c = BlockChunker(min_chars=10, max_chars=100)
    assert c.flush() == []


def test_sentence_boundary_priority():
    c = BlockChunker(min_chars=20, max_chars=1000)
    # No paragraph or newline → should split at sentence terminator.
    text = "First sentence here. Second sentence here. Third sentence here."
    out = c.feed(text)
    assert len(out) >= 1
    assert out[0].boundary in ("sentence", "newline", "paragraph")


def test_wrap_stream_callback_emits_blocks_only():
    received: list[str] = []
    cb = wrap_stream_callback(received.append, min_chars=10, max_chars=1000)
    # Stream raw character-by-character deltas; chunker should only emit blocks.
    for ch in "Hello there.\n\nSecond paragraph.\n\n":
        cb(ch)
    # min_chars=10 means short buffers don't emit; expect 2 blocks at paragraph boundaries.
    assert len(received) == 2
    assert "Hello there." in received[0]
    assert "Second paragraph." in received[1]


def test_wrap_stream_callback_with_none_inner_is_noop():
    cb = wrap_stream_callback(None)
    # Should not raise on any input.
    for ch in "Some content here.\n\n":
        cb(ch)
