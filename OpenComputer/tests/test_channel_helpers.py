"""Tests for plugin_sdk.channel_helpers."""

from __future__ import annotations

import asyncio

import pytest

from plugin_sdk.channel_helpers import (
    MessageDeduplicator,
    TextBatchAggregator,
    ThreadParticipationTracker,
    redact_phone,
    strip_markdown,
)

# ---------------------------------------------------------------------------
# MessageDeduplicator
# ---------------------------------------------------------------------------


def test_message_deduplicator_first_seen_is_new():
    dedup = MessageDeduplicator(max_size=100, ttl=60.0)
    assert dedup.is_new("msg-1") is True
    assert dedup.is_new("msg-1") is False  # second call: already seen


def test_message_deduplicator_ttl_expiry(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("plugin_sdk.channel_helpers.time.time", lambda: now[0])
    dedup = MessageDeduplicator(max_size=100, ttl=60.0)
    dedup.is_new("msg-1")
    now[0] += 30
    assert dedup.is_new("msg-1") is False  # within TTL
    now[0] += 31
    assert dedup.is_new("msg-1") is True  # past TTL, fresh


def test_message_deduplicator_max_size_eviction():
    dedup = MessageDeduplicator(max_size=3, ttl=300.0)
    for i in range(5):
        dedup.is_new(f"msg-{i}")
    # First two should have been evicted
    assert dedup.is_new("msg-0") is True
    assert dedup.is_new("msg-1") is True
    assert dedup.is_new("msg-4") is False


def test_message_deduplicator_ttl_zero_disables():
    dedup = MessageDeduplicator(max_size=100, ttl=0.0)
    assert dedup.is_new("msg-1") is True
    assert dedup.is_new("msg-1") is True  # always new when ttl=0


# ---------------------------------------------------------------------------
# TextBatchAggregator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_batch_aggregator_single_dispatch():
    received: list[str] = []

    async def handler(text: str) -> None:
        received.append(text)

    agg = TextBatchAggregator(
        handler, batch_delay=0.05, split_delay=0.1, split_threshold=4000
    )
    await agg.submit("chat-1", "hello")
    await asyncio.sleep(0.15)
    assert received == ["hello"]


@pytest.mark.asyncio
async def test_text_batch_aggregator_combines_within_window():
    received: list[str] = []

    async def handler(text: str) -> None:
        received.append(text)

    agg = TextBatchAggregator(
        handler, batch_delay=0.1, split_delay=0.2, split_threshold=4000
    )
    await agg.submit("chat-1", "part 1")
    await asyncio.sleep(0.02)
    await agg.submit("chat-1", "part 2")
    await asyncio.sleep(0.2)
    assert received == ["part 1\npart 2"]


@pytest.mark.asyncio
async def test_text_batch_aggregator_per_chat_isolation():
    received: list[tuple[str, str]] = []

    async def handler(text: str, chat: str = "") -> None:
        received.append((chat, text))

    agg = TextBatchAggregator(
        handler,
        batch_delay=0.05,
        split_delay=0.1,
        split_threshold=4000,
        chat_aware=True,
    )
    await agg.submit("chat-A", "hello A")
    await agg.submit("chat-B", "hello B")
    await asyncio.sleep(0.15)
    chats = {c for c, _ in received}
    assert chats == {"chat-A", "chat-B"}


@pytest.mark.asyncio
async def test_text_batch_aggregator_adaptive_split_near_limit():
    # When the latest chunk is large (> threshold), the aggregator should
    # extend the wait window, anticipating a continuation chunk.
    received: list[str] = []

    async def handler(text: str) -> None:
        received.append(text)

    agg = TextBatchAggregator(
        handler, batch_delay=0.05, split_delay=0.3, split_threshold=10
    )
    big = "x" * 20  # > split_threshold
    await agg.submit("chat-1", big)
    # Within the LONG split_delay window the next chunk should still merge
    await asyncio.sleep(0.1)
    await agg.submit("chat-1", "y" * 5)
    await asyncio.sleep(0.4)
    # Both chunks should have been combined into one dispatch
    assert received == [big + "\n" + "y" * 5]


# ---------------------------------------------------------------------------
# strip_markdown
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "input_text,expected",
    [
        ("**bold**", "bold"),
        ("*italic*", "italic"),
        ("__underline__", "underline"),
        ("_italic_", "italic"),
        ("~~strike~~", "strike"),
        ("# Heading", "Heading"),
        ("## Heading 2", "Heading 2"),
        ("`code`", "code"),
        ("[link text](https://example.com)", "link text"),
        ("```python\nx = 1\n```", "x = 1"),
        ("plain text", "plain text"),
        ("multi\n**line**\nformat", "multi\nline\nformat"),
    ],
)
def test_strip_markdown_basic(input_text, expected):
    assert strip_markdown(input_text) == expected


# ---------------------------------------------------------------------------
# redact_phone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phone,expected",
    [
        ("+15551234567", "+1***4567"),
        ("+919876543210", "+91***3210"),
        ("+447911123456", "+44***3456"),
        ("5551234567", "***4567"),  # no country code
        ("", ""),
        (None, ""),
    ],
)
def test_redact_phone(phone, expected):
    assert redact_phone(phone) == expected


def test_redact_phone_short_after_country_code():
    # "+12" — only 2 digits total. CC=first digit, rest=1 digit (<4).
    assert redact_phone("+12") == "+1***"


def test_redact_phone_short_input():
    assert redact_phone("12") == "***"


# ---------------------------------------------------------------------------
# ThreadParticipationTracker
# ---------------------------------------------------------------------------


def test_thread_tracker_records_and_persists(tmp_path):
    tracker = ThreadParticipationTracker(
        "discord", profile_home=tmp_path, max_tracked=10
    )
    tracker.record("thread-1")
    tracker.record("thread-2")
    assert tracker.is_participating("thread-1")
    assert tracker.is_participating("thread-2")
    # Reload from disk
    tracker2 = ThreadParticipationTracker(
        "discord", profile_home=tmp_path, max_tracked=10
    )
    assert tracker2.is_participating("thread-1")
    assert tracker2.is_participating("thread-2")


def test_thread_tracker_max_bound_evicts_oldest(tmp_path):
    tracker = ThreadParticipationTracker(
        "matrix", profile_home=tmp_path, max_tracked=3
    )
    for i in range(5):
        tracker.record(f"thread-{i}")
    assert not tracker.is_participating("thread-0")
    assert not tracker.is_participating("thread-1")
    assert tracker.is_participating("thread-4")


def test_thread_tracker_per_platform_isolated(tmp_path):
    a = ThreadParticipationTracker(
        "discord", profile_home=tmp_path, max_tracked=10
    )
    b = ThreadParticipationTracker(
        "matrix", profile_home=tmp_path, max_tracked=10
    )
    a.record("shared-id")
    assert a.is_participating("shared-id")
    assert not b.is_participating("shared-id")


def test_thread_tracker_no_op_on_duplicate_record(tmp_path):
    tracker = ThreadParticipationTracker(
        "telegram", profile_home=tmp_path, max_tracked=5
    )
    tracker.record("thread-X")
    tracker.record("thread-X")
    tracker.record("thread-X")
    # Still only one entry.
    assert tracker._threads == ["thread-X"]


def test_thread_tracker_handles_int_thread_id(tmp_path):
    tracker = ThreadParticipationTracker(
        "discord", profile_home=tmp_path, max_tracked=10
    )
    tracker.record(12345)  # type: ignore[arg-type]
    assert tracker.is_participating("12345")
    assert tracker.is_participating(12345)  # type: ignore[arg-type]
